"""Cloud-agent dispatch backend for asynchronous Codex on-call runs.

The GitHub Actions backend hands the whole analysis to a workflow and never
hears from it again. This backend keeps the loop in the relay: it creates a
job on the FreeInference cloud agent control plane
(``/v1/agent/service/oncall/*``), and the relay's worker polls the job to a
terminal state, validates the result, and posts it into the alert's Slack
thread itself.

What that buys, compared to the workflow: the analysis runs on the
platform's own runner host instead of billed GitHub-hosted minutes; the
sandbox's model credential is a short-lived inference grant instead of a
long-lived Actions secret; spend lands in ``api_logs.agent_job_id``; and the
checkout is pinned to the commit ``base_ref`` named at creation.

The relay's credential here is the control plane's dedicated
``AGENT_ONCALL_DISPATCH_TOKEN`` — one credential, one door. It can create,
read, and cancel on-call jobs for the configured service account and nothing
else; it is not a gateway key and buys no inference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from serving.oncall.analysis import render_prompt, render_schema
from serving.oncall.models import sanitize_for_agent

if TYPE_CHECKING:
    from serving.oncall.config import OnCallSettings
    from serving.oncall.models import AlertEvent


class CloudAgentError(RuntimeError):
    """Raised when the control plane refuses or cannot answer a call."""


#: Job states the platform reports that end the relay's poll loop. Anything
#: else — ``queued``, ``running``, ``publishing``, or a state this relay has
#: never heard of — keeps polling until the deadline, so a new intermediate
#: state on the platform degrades to patience rather than to a wrong verdict.
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

#: Bound on event pagination. A 15-minute Codex run produces hundreds of
#: events, not tens of thousands; a job that somehow exceeds this is refused
#: as unusable rather than read forever.
_MAX_EVENT_PAGES = 50
_EVENT_PAGE_LIMIT = 1000


class CloudAgentClient:
    """Minimal client for the control plane's on-call service surface."""

    def __init__(self, settings: OnCallSettings, *, timeout_seconds: float = 15.0) -> None:
        self._settings = settings
        self._timeout_seconds = timeout_seconds

    def _base_url(self) -> str:
        base = self._settings.agent_base_url.strip().rstrip("/")
        if not base:
            raise CloudAgentError(
                "CODEX_ONCALL_AGENT_BASE_URL is unset; set it to the cloud agent "
                "control plane origin"
            )
        return base

    def _headers(self) -> dict[str, str]:
        token = self._settings.agent_dispatch_token.get_secret_value().strip()
        if not token:
            raise CloudAgentError("CODEX_ONCALL_AGENT_DISPATCH_TOKEN is unset")
        return {"Authorization": f"Bearer {token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url()}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.request(
                    method, url, headers=self._headers(), json=json_body, params=params
                )
        except httpx.HTTPError as exc:
            raise CloudAgentError(f"cloud agent control plane unreachable: {exc}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    error = payload.get("detail")
                    if isinstance(error, dict):
                        error = error.get("error") or error
                    if isinstance(error, dict):
                        detail = str(error.get("message") or "")
                    elif isinstance(error, str):
                        detail = error
            except ValueError:
                detail = ""
            raise CloudAgentError(
                f"cloud agent control plane answered HTTP {response.status_code} for {path}"
                + (f": {detail}" if detail else "")
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudAgentError(
                f"cloud agent control plane returned non-JSON for {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise CloudAgentError(f"cloud agent control plane returned {type(payload).__name__}")
        return payload

    def build_job_body(self, event: AlertEvent) -> dict[str, Any]:
        """Build the job-creation body — sanitized alert only, never ``slack_text``.

        The prompt is rendered relay-side (the workflow rendered it
        runner-side), from the same template and schema, so the model sees the
        same task whichever backend runs it. Slack coordinates deliberately do
        not travel: the relay owns the thread, and a sandbox that never sees a
        channel id cannot post to one.
        """
        safe_alert = sanitize_for_agent(event.model_dump(mode="json", exclude={"slack_text"}))
        assert isinstance(safe_alert, dict)
        prompt = render_prompt(safe_alert, render_schema())
        body: dict[str, Any] = {
            "repo": self._settings.oncall_repo,
            "task_prompt": prompt,
            "runtime": self._settings.agent_runtime.strip(),
            "model": self._settings.codex_model.strip(),
            # Explicit none: an on-call analysis is read-only and needs no
            # tools beyond the shell; the deployment's default MCP servers
            # must not leak into it.
            "mcp_servers": [],
            "metadata": {
                "fingerprint": event.fingerprint,
                "alert_id": event.alert_id,
                "source": event.source,
                "environment": event.environment,
            },
        }
        base_ref = self._settings.agent_base_ref.strip()
        if base_ref:
            body["base_ref"] = base_ref
        return body

    async def create_job(self, event: AlertEvent) -> str:
        """Create one analysis job; return the platform's job id."""
        payload = await self._request(
            "POST", "/v1/agent/service/oncall/jobs", json_body=self.build_job_body(event)
        )
        job_id = str(payload.get("id") or "").strip()
        if not job_id:
            raise CloudAgentError("cloud agent control plane returned a job without an id")
        return job_id

    async def get_job_state(self, job_id: str) -> str:
        """Return the job's current state string."""
        payload = await self._request("GET", f"/v1/agent/service/oncall/jobs/{job_id}")
        return str(payload.get("state") or "").strip() or "unknown"

    async def list_events(self, job_id: str) -> list[dict[str, Any]]:
        """Return the job's full normalized event log, paged to completion."""
        events: list[dict[str, Any]] = []
        after = 0
        for _ in range(_MAX_EVENT_PAGES):
            payload = await self._request(
                "GET",
                f"/v1/agent/service/oncall/jobs/{job_id}/events",
                params={"after": after, "limit": _EVENT_PAGE_LIMIT},
            )
            page = payload.get("events")
            if not isinstance(page, list) or not page:
                return events
            events.extend(item for item in page if isinstance(item, dict))
            cursor = payload.get("next_cursor")
            if not isinstance(cursor, int) or cursor <= after:
                return events
            after = cursor
        raise CloudAgentError(f"job {job_id} event log exceeds {_MAX_EVENT_PAGES} pages")

    async def cancel_job(self, job_id: str) -> bool:
        """Request cancellation; best-effort, False when the call fails.

        Cancellation accelerates something the platform's grant TTL bounds
        anyway, so a failure here must not take down the caller — which is a
        timeout path with a failure notice still to post.
        """
        try:
            await self._request("POST", f"/v1/agent/service/oncall/jobs/{job_id}/cancel")
        except CloudAgentError:
            return False
        return True


class CloudAgentDispatcher:
    """``AnalysisDispatcher`` that hands the analysis to the cloud agent.

    Returns the platform job id so the service parks the relay job in its
    ``await_result`` stage — unlike the GitHub dispatcher, which returns
    ``None`` because the workflow owns everything after a successful dispatch.
    """

    def __init__(self, client: CloudAgentClient) -> None:
        self._client = client

    async def dispatch(self, event: AlertEvent, slack_thread_ts: str) -> str:
        """Create the analysis job; return its id for the poll loop."""
        del slack_thread_ts  # The relay posts the result itself, later.
        return await self._client.create_job(event)
