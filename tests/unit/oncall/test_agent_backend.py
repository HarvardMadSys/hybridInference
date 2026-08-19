"""Tests for the cloud-agent dispatch backend and its control-plane client."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from serving.oncall.agent_backend import (
    CloudAgentClient,
    CloudAgentDispatcher,
    CloudAgentError,
)
from serving.oncall.config import OnCallSettings
from serving.oncall.models import AlertEvent


def settings(**overrides) -> OnCallSettings:
    base: dict = {
        "relay_token": SecretStr("relay"),
        "slack_bot_token": SecretStr("xoxb-token"),
        "slack_channel_id": "C123",
        "dispatch_backend": "cloud-agent",
        "agent_base_url": "https://agent.example.org/",
        "agent_dispatch_token": SecretStr("oncall-dispatch-secret"),
        "github_repository": "example-org/example-repo",
        "codex_model": "glm-5.2",
    }
    base.update(overrides)
    return OnCallSettings(**base)


def event() -> AlertEvent:
    return AlertEvent(
        alert_id="alert-1",
        fingerprint="gateway:production:test",
        source="test",
        status="firing",
        severity="error",
        title="Provider failed",
        environment="production",
        occurred_at=datetime.now(timezone.utc),
        summary="Bearer abcdefghijklmnop",
        context={"api_key": "sk-secret", "provider": "openai"},
        slack_text="this must never leave the relay",
    )


class FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class FakeHttpClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


def _client_with(responses: list[FakeResponse]) -> tuple[CloudAgentClient, FakeHttpClient]:
    return CloudAgentClient(settings()), FakeHttpClient(responses)


def test_job_body_is_sanitized_and_carries_no_slack_coordinates():
    body = CloudAgentClient(settings()).build_job_body(event())

    assert body["repo"] == "example-org/example-repo"
    assert body["runtime"] == "codex"
    assert body["model"] == "glm-5.2"
    assert body["base_ref"] == "dev"
    assert body["mcp_servers"] == []
    assert body["metadata"]["fingerprint"] == "gateway:production:test"
    # The prompt embeds the sanitized alert: secrets redacted, slack_text and
    # any Slack coordinates absent — a sandbox that never sees a channel id
    # cannot post to one.
    serialized = json.dumps(body)
    assert "sk-secret" not in serialized
    assert "[REDACTED]" in body["task_prompt"]
    assert "this must never leave the relay" not in serialized
    assert "slack" not in serialized.lower()
    assert "<untrusted_alert_json>" in body["task_prompt"]


def test_job_body_prefers_agent_repo_override():
    body = CloudAgentClient(settings(agent_repo="other-org/other-repo")).build_job_body(event())
    assert body["repo"] == "other-org/other-repo"


async def test_create_job_posts_and_returns_id():
    client, fake = _client_with([FakeResponse(201, {"id": "ajob_0042", "state": "queued"})])
    with patch("serving.oncall.agent_backend.httpx.AsyncClient", return_value=fake):
        job_id = await client.create_job(event())

    assert job_id == "ajob_0042"
    method, url, kwargs = fake.calls[0]
    assert (method, url) == ("POST", "https://agent.example.org/v1/agent/service/oncall/jobs")
    assert kwargs["headers"]["Authorization"] == "Bearer oncall-dispatch-secret"


async def test_create_job_surfaces_control_plane_refusal():
    refusal = {"detail": {"error": {"type": "model_not_available", "message": "no such model"}}}
    client, fake = _client_with([FakeResponse(400, refusal)])
    with (
        patch("serving.oncall.agent_backend.httpx.AsyncClient", return_value=fake),
        pytest.raises(CloudAgentError, match=r"400.*no such model"),
    ):
        await client.create_job(event())


async def test_get_job_state_reads_state():
    client, fake = _client_with([FakeResponse(200, {"id": "ajob_1", "state": "running"})])
    with patch("serving.oncall.agent_backend.httpx.AsyncClient", return_value=fake):
        assert await client.get_job_state("ajob_1") == "running"


async def test_list_events_pages_until_cursor_stops():
    pages = [
        FakeResponse(
            200,
            {
                "events": [{"id": 1, "event_type": "tool_result", "payload": {"exit_code": 0}}],
                "next_cursor": 1,
            },
        ),
        FakeResponse(
            200,
            {
                "events": [{"id": 2, "event_type": "message", "payload": {"text": "{}"}}],
                "next_cursor": 2,
            },
        ),
        FakeResponse(200, {"events": [], "next_cursor": 2}),
    ]
    client, fake = _client_with(pages)
    with patch("serving.oncall.agent_backend.httpx.AsyncClient", return_value=fake):
        events = await client.list_events("ajob_1")

    assert [item["id"] for item in events] == [1, 2]
    # Each page asked after the cursor the previous one returned.
    afters = [call[2]["params"]["after"] for call in fake.calls]
    assert afters == [0, 1, 2]


async def test_cancel_job_is_best_effort():
    ok, fake_ok = _client_with([FakeResponse(200, {"id": "ajob_1", "state": "cancelled"})])
    with patch("serving.oncall.agent_backend.httpx.AsyncClient", return_value=fake_ok):
        assert await ok.cancel_job("ajob_1") is True

    failing, fake_bad = _client_with([FakeResponse(503, None)])
    with patch("serving.oncall.agent_backend.httpx.AsyncClient", return_value=fake_bad):
        assert await failing.cancel_job("ajob_1") is False


async def test_dispatcher_returns_platform_job_id():
    client, fake = _client_with([FakeResponse(201, {"id": "ajob_0042", "state": "queued"})])
    with patch("serving.oncall.agent_backend.httpx.AsyncClient", return_value=fake):
        handle = await CloudAgentDispatcher(client).dispatch(event(), "171.1")
    assert handle == "ajob_0042"


def test_unconfigured_client_refuses_before_the_wire():
    with pytest.raises(CloudAgentError, match="AGENT_BASE_URL"):
        CloudAgentClient(settings(agent_base_url=""))._base_url()
    with pytest.raises(CloudAgentError, match="DISPATCH_TOKEN"):
        CloudAgentClient(settings(agent_dispatch_token=SecretStr("")))._headers()


def test_configured_property_by_backend():
    # cloud-agent mode: needs the agent pair, not the GitHub trio.
    assert settings().configured is True
    assert settings(agent_base_url="").configured is False
    assert settings(agent_dispatch_token=SecretStr("")).configured is False
    assert settings(github_repository="", agent_repo="o/r").configured is True
    assert settings(github_repository="").configured is False
    # github mode still demands its own credentials.
    github = settings(
        dispatch_backend="github",
        github_token=SecretStr("gh"),
        model_base_url="https://gateway.example.com/v1",
    )
    assert github.configured is True
    assert settings(dispatch_backend="github").configured is False
