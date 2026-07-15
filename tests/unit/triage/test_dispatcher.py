"""Tests for the GitHub Actions dispatch hand-off."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from serving.triage.config import TriageSettings
from serving.triage.dispatcher import DispatchError, GitHubDispatcher
from serving.triage.models import AlertEvent


def settings(**overrides) -> TriageSettings:
    base: dict = {
        "relay_token": SecretStr("relay"),
        "slack_bot_token": SecretStr("xoxb-token"),
        "slack_channel_id": "C123",
        "github_token": SecretStr("github-secret"),
        "github_repository": "HarvardMadSys/hybridInference",
        "codex_model": "deepseek-v4-flash",
        "hybrid_inference_base_url": "https://freeinference.org/v1/",
    }
    base.update(overrides)
    return TriageSettings(**base)


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
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeHttpClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_payload_is_sanitized_and_excludes_slack_text():
    payload = GitHubDispatcher(settings()).build_payload(event(), "171.1")

    triage = payload["client_payload"]["triage"]
    assert payload["event_type"] == "codex-triage"
    assert triage["alert"]["context"]["api_key"] == "[REDACTED]"
    assert "Bearer [REDACTED]" in triage["alert"]["summary"]
    assert "slack_text" not in triage["alert"]
    assert triage["fingerprint"] == "gateway:production:test"
    assert triage["slack_channel_id"] == "C123"
    assert triage["slack_thread_ts"] == "171.1"
    assert triage["model"] == "deepseek-v4-flash"
    assert triage["responses_base_url"] == "https://freeinference.org/v1"
    assert "github-secret" not in json.dumps(payload)


async def test_dispatch_posts_repository_dispatch():
    fake = FakeHttpClient(FakeResponse(204))
    with patch("serving.triage.dispatcher.httpx.AsyncClient", return_value=fake):
        await GitHubDispatcher(settings()).dispatch(event(), "171.1")

    url, kwargs = fake.calls[0]
    assert url == "https://api.github.com/repos/HarvardMadSys/hybridInference/dispatches"
    assert kwargs["headers"]["Authorization"] == "Bearer github-secret"
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert kwargs["json"]["event_type"] == "codex-triage"


async def test_dispatch_raises_on_unexpected_status():
    fake = FakeHttpClient(FakeResponse(422))
    with (
        patch("serving.triage.dispatcher.httpx.AsyncClient", return_value=fake),
        pytest.raises(DispatchError, match="422"),
    ):
        await GitHubDispatcher(settings()).dispatch(event(), "171.1")


def test_configured_requires_github_credentials():
    assert settings().configured is True
    assert settings(github_token=SecretStr("")).configured is False
    assert settings(github_repository="").configured is False
