"""GitHub Actions hand-off for asynchronous Codex on-call runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from serving.oncall.models import AlertEvent, sanitize_for_agent

if TYPE_CHECKING:
    from serving.oncall.config import OnCallSettings


class DispatchError(RuntimeError):
    """Raised when GitHub refuses or cannot receive a dispatch."""


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

    async def dispatch(self, event: AlertEvent, slack_thread_ts: str) -> None:
        """POST a ``repository_dispatch``; raise :class:`DispatchError` on failure."""
        repository = self._settings.github_repository.strip()
        api_base = self._settings.github_api_base_url.strip().rstrip("/")
        url = f"{api_base}/repos/{repository}/dispatches"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._settings.github_token.get_secret_value().strip()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    json=self.build_payload(event, slack_thread_ts),
                )
        except httpx.HTTPError as exc:
            raise DispatchError("GitHub dispatch request failed") from exc
        if response.status_code != 204:
            raise DispatchError(f"GitHub dispatch returned HTTP {response.status_code}")
