"""Small Slack Web API client used by the relay."""

from __future__ import annotations

import httpx


class SlackDeliveryError(RuntimeError):
    """Raised when Slack rejects or cannot receive a message."""


class SlackClient:
    """Post top-level messages and thread replies with a bot token."""

    def __init__(self, token: str, channel_id: str, *, timeout_seconds: float = 10.0) -> None:
        self._token = token
        self._channel_id = channel_id
        self._timeout_seconds = timeout_seconds

    async def post(self, text: str, *, thread_ts: str | None = None) -> str:
        """Post a message and return its Slack timestamp."""
        payload: dict[str, str] = {"channel": self._channel_id, "text": text[:40_000]}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json=payload,
                )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SlackDeliveryError("Slack chat.postMessage request failed") from exc
        if not isinstance(body, dict):
            raise SlackDeliveryError("Slack chat.postMessage returned an invalid response")
        if not body.get("ok") or not body.get("ts"):
            error = str(body.get("error") or "unknown_error")
            raise SlackDeliveryError(f"Slack chat.postMessage rejected the request: {error}")
        return str(body["ts"])
