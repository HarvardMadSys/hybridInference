"""Tests for Slack Web API delivery and thread replies."""

from unittest.mock import patch

import pytest

from serving.triage.slack import SlackClient, SlackDeliveryError


class FakeResponse:
    def __init__(self, body: object) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._body


class FakeHttpClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


async def test_slack_client_posts_thread_reply_and_returns_timestamp():
    fake = FakeHttpClient(FakeResponse({"ok": True, "ts": "123.46"}))
    with patch("serving.triage.slack.httpx.AsyncClient", return_value=fake):
        timestamp = await SlackClient("xoxb-token", "C123").post(
            "analysis",
            thread_ts="123.45",
        )

    assert timestamp == "123.46"
    _, kwargs = fake.calls[0]
    assert kwargs["headers"] == {"Authorization": "Bearer xoxb-token"}
    assert kwargs["json"] == {
        "channel": "C123",
        "text": "analysis",
        "thread_ts": "123.45",
    }


async def test_slack_client_rejects_non_object_response():
    fake = FakeHttpClient(FakeResponse([]))
    with (
        patch("serving.triage.slack.httpx.AsyncClient", return_value=fake),
        pytest.raises(SlackDeliveryError, match="invalid response"),
    ):
        await SlackClient("xoxb-token", "C123").post("analysis")
