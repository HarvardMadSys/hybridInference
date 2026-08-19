"""GitHub Actions hand-off for asynchronous Codex on-call runs."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import httpx

from serving.oncall.models import AlertEvent, sanitize_for_agent

if TYPE_CHECKING:
    from serving.oncall.config import OnCallSettings


class DispatchError(RuntimeError):
    """Raised when GitHub refuses or cannot receive a dispatch."""


# repository_dispatch carries no run id, so delivery confirmation matches
# "any repository_dispatch run newer than the pre-dispatch snapshot" against
# this many of the newest runs. That is enough to catch a dispatch GitHub
# accepted (HTTP 204) while no workflow actually listens for the event.
RUN_SNAPSHOT_WINDOW = 20


def _parse_github_time(value: str) -> float:
    """Parse a GitHub ISO-8601 timestamp (e.g. 2026-08-18T10:00:00Z) to epoch."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class GitHubDispatcher:
    """Trigger the ``codex-oncall`` workflow through ``repository_dispatch``.

    The relay never runs Codex itself: it posts the original alert to Slack,
    then hands the sanitized alert to a GitHub Actions workflow that checks out
    the current ``dev`` branch, runs ``codex exec`` against the relay-configured
    Responses API endpoint (in practice the gateway), and replies in the same
    Slack thread.
    """

    def __init__(self, settings: OnCallSettings, *, timeout_seconds: float = 15.0) -> None:
        self._settings = settings
        self._timeout_seconds = timeout_seconds

    def build_payload(self, event: AlertEvent, slack_thread_ts: str) -> dict[str, Any]:
        """Build the dispatch body — sanitized alert only, never ``slack_text``.

        Raises if no gateway was configured. The setting used to default to one
        deployment's public URL, so an operator who never configured on-call
        would have sent their analysis job at someone else's gateway; an empty
        value has to stop here rather than dispatch to nowhere.
        """
        base_url = self._settings.model_base_url.strip().rstrip("/")
        if not base_url:
            raise DispatchError(
                "CODEX_ONCALL_MODEL_BASE_URL is unset; set it to a gateway that "
                "serves /v1/responses and is reachable from GitHub-hosted runners"
            )
        safe_alert = sanitize_for_agent(event.model_dump(mode="json", exclude={"slack_text"}))
        return {
            "event_type": self._settings.dispatch_event_type.strip(),
            "client_payload": {
                "oncall": {
                    "alert": safe_alert,
                    "fingerprint": event.fingerprint,
                    "alert_id": event.alert_id,
                    "slack_channel_id": self._settings.slack_channel_id.strip(),
                    "slack_thread_ts": slack_thread_ts,
                    "model": self._settings.codex_model.strip(),
                    "base_url": base_url,
                }
            },
        }

    def _headers(self) -> dict[str, str]:
        """Return the common GitHub REST API headers."""
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._settings.github_token.get_secret_value().strip()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _runs_url(self, *, per_page: int) -> str:
        repository = self._settings.github_repository.strip()
        api_base = self._settings.github_api_base_url.strip().rstrip("/")
        return (
            f"{api_base}/repos/{repository}/actions/runs"
            f"?event=repository_dispatch&per_page={per_page}"
        )

    async def dispatch(self, event: AlertEvent, slack_thread_ts: str) -> None:
        """POST a ``repository_dispatch``; raise :class:`DispatchError` on failure."""
        url = f"{self._settings.github_api_base_url.strip().rstrip('/')}/repos/{self._settings.github_repository.strip()}/dispatches"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    url,
                    headers=self._headers(),
                    json=self.build_payload(event, slack_thread_ts),
                )
        except httpx.HTTPError as exc:
            raise DispatchError("GitHub dispatch request failed") from exc
        if response.status_code != 204:
            raise DispatchError(f"GitHub dispatch returned HTTP {response.status_code}")

    async def latest_run_id(self) -> int | None:
        """Return the newest repository_dispatch run id, or None when unknown.

        Best-effort snapshot taken just before a dispatch so the confirmation
        poll can recognize the run this dispatch creates. Never raises: a
        failed probe must not block the hand-off itself. Returns 0 when the
        workflow has never run, so that any first run confirms.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(self._runs_url(per_page=1), headers=self._headers())
            if response.status_code != 200:
                return None
            runs = response.json().get("workflow_runs") or []
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(runs, list) or not runs:
            return 0
        run_id = runs[0].get("id")
        return int(run_id) if isinstance(run_id, int) else None

    async def confirm_run_started(
        self, *, before_run_id: int | None, dispatched_at: float
    ) -> tuple[bool, str | None]:
        """Return whether a workflow run appeared after the dispatch, and its URL.

        Coarse by design: ``repository_dispatch`` never returns a run id, so
        this matches "any repository_dispatch run newer than the snapshot
        taken before the dispatch" rather than the exact run. That is exactly
        the signal needed to catch the silent-drop mode where GitHub accepts
        the event (HTTP 204) but no workflow listens for it any more.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(
                    self._runs_url(per_page=RUN_SNAPSHOT_WINDOW), headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise DispatchError("GitHub Actions run list request failed") from exc
        if response.status_code != 200:
            raise DispatchError(f"GitHub Actions run list returned HTTP {response.status_code}")
        try:
            runs = response.json().get("workflow_runs") or []
        except ValueError as exc:
            raise DispatchError("GitHub Actions run list returned an invalid response") from exc
        if not isinstance(runs, list):
            raise DispatchError("GitHub Actions run list returned an invalid response")

        for run in runs:
            run_id = run.get("id")
            if not isinstance(run_id, int):
                continue
            if before_run_id is not None:
                if run_id > before_run_id:
                    return self._run_result(run)
                continue
            # No snapshot (the probe failed before dispatch): fall back to
            # creation time. The one-minute tolerance absorbs GitHub clock
            # skew; this path is intentionally looser than the id comparison.
            created = run.get("created_at")
            if not isinstance(created, str):
                continue
            try:
                if _parse_github_time(created) >= dispatched_at - 60.0:
                    return self._run_result(run)
            except ValueError:
                continue
        return False, None

    @staticmethod
    def _run_result(run: dict[str, Any]) -> tuple[bool, str | None]:
        """Return (True, run URL) for a matched run."""
        url = run.get("html_url")
        return True, str(url) if url else None
