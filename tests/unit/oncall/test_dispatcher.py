"""Tests for the GitHub Actions dispatch hand-off."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from serving.oncall.config import OnCallSettings
from serving.oncall.dispatcher import DispatchError, GitHubDispatcher
from serving.oncall.models import AlertEvent


def settings(**overrides) -> OnCallSettings:
    base: dict = {
        "relay_token": SecretStr("relay"),
        "slack_bot_token": SecretStr("xoxb-token"),
        "slack_channel_id": "C123",
        "github_token": SecretStr("github-secret"),
        "github_repository": "example-org/example-repo",
        "codex_model": "glm-5.1",
        "model_base_url": "https://gateway.example.com/v1/",
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

    oncall = payload["client_payload"]["oncall"]
    assert payload["event_type"] == "codex-oncall"
    assert oncall["alert"]["context"]["api_key"] == "[REDACTED]"
    assert "Bearer [REDACTED]" in oncall["alert"]["summary"]
    assert "slack_text" not in oncall["alert"]
    assert oncall["fingerprint"] == "gateway:production:test"
    assert oncall["slack_channel_id"] == "C123"
    assert oncall["slack_thread_ts"] == "171.1"
    assert oncall["model"] == "glm-5.1"
    assert oncall["base_url"] == "https://gateway.example.com/v1"
    assert "github-secret" not in json.dumps(payload)


async def test_dispatch_posts_repository_dispatch():
    fake = FakeHttpClient(FakeResponse(204))
    with patch("serving.oncall.dispatcher.httpx.AsyncClient", return_value=fake):
        await GitHubDispatcher(settings()).dispatch(event(), "171.1")

    url, kwargs = fake.calls[0]
    assert url == "https://api.github.com/repos/example-org/example-repo/dispatches"
    assert kwargs["headers"]["Authorization"] == "Bearer github-secret"
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert kwargs["json"]["event_type"] == "codex-oncall"


async def test_dispatch_raises_on_unexpected_status():
    fake = FakeHttpClient(FakeResponse(422))
    with (
        patch("serving.oncall.dispatcher.httpx.AsyncClient", return_value=fake),
        pytest.raises(DispatchError, match="422"),
    ):
        await GitHubDispatcher(settings()).dispatch(event(), "171.1")


def test_configured_requires_github_credentials():
    assert settings().configured is True
    assert settings(github_token=SecretStr("")).configured is False
    assert settings(github_repository="").configured is False


def test_defaults_point_at_the_gateway_responses_api(monkeypatch):
    # Codex is Responses-API-only (openai/codex#7782), so the base URL must be
    # the gateway. glm-5.2 stays the default until the H200 V4 parser fix
    # (PR #939) is deployed and verified; then flip to deepseek-v4-flash.
    for var in ("CODEX_ONCALL_CODEX_MODEL", "CODEX_ONCALL_MODEL_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    defaults = OnCallSettings()
    assert defaults.codex_model == "glm-5.2"
    # No default any more: it used to be one deployment's public URL, so
    # every other operator dispatched their analysis at it.
    assert defaults.model_base_url == ""
