"""Tests for serving.observability.alerts.alert_slack."""

from unittest.mock import AsyncMock, patch

import pytest

from serving.observability.alerts import (
    AlertSeverity,
    _base_url,
    _detect_environment,
    _format_message,
    alert_slack,
    reset_dedupe_state,
    server_info,
)


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.delenv("CODEX_TRIAGE_RELAY_URL", raising=False)
    monkeypatch.delenv("CODEX_TRIAGE_RELAY_TOKEN", raising=False)
    reset_dedupe_state()
    yield
    reset_dedupe_state()


async def test_alert_slack_no_op_when_webhook_unset(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock()) as mock_post:
        await alert_slack(AlertSeverity.ERROR, "test", {"k": "v"})
        mock_post.assert_not_called()


async def test_alert_slack_posts_when_webhook_set(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(return_value=True),
    ) as mock_post:
        await alert_slack(AlertSeverity.ERROR, "test title", {"foo": "bar"})
        mock_post.assert_awaited_once()
        args, _ = mock_post.call_args
        url, message = args
        assert url == "https://hooks.slack.com/x"
        assert "test title" in message
        assert "Foo" in message and "bar" in message


async def test_alert_slack_prefers_triage_relay(monkeypatch):
    monkeypatch.setenv("CODEX_TRIAGE_RELAY_URL", "https://triage.internal/")
    monkeypatch.setenv("CODEX_TRIAGE_RELAY_TOKEN", "relay-secret")
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/fallback")
    with (
        patch(
            "serving.observability.alerts._post_to_triage",
            new=AsyncMock(return_value=True),
        ) as mock_triage,
        patch("serving.observability.alerts._post_to_slack", new=AsyncMock()) as mock_slack,
    ):
        sent = await alert_slack(
            AlertSeverity.ERROR,
            "Provider failed",
            {"provider": "openai", "api_key": "must-not-leak"},
            dedupe_key="provider:openai",
        )

    assert sent is True
    mock_slack.assert_not_called()
    relay_url, token, event = mock_triage.call_args.args
    assert relay_url == "https://triage.internal/"
    assert token == "relay-secret"
    assert event.fingerprint.endswith(":provider:openai")
    assert event.context["provider"] == "openai"
    assert event.context["api_key"] == "[REDACTED]"


async def test_alert_slack_falls_back_when_triage_relay_fails(monkeypatch):
    monkeypatch.setenv("CODEX_TRIAGE_RELAY_URL", "https://triage.internal")
    monkeypatch.setenv("CODEX_TRIAGE_RELAY_TOKEN", "relay-secret")
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/fallback")
    with (
        patch(
            "serving.observability.alerts._post_to_triage",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_slack,
    ):
        sent = await alert_slack(AlertSeverity.ERROR, "Provider failed", {})

    assert sent is True
    mock_slack.assert_awaited_once()
    assert mock_slack.call_args.args[0] == "https://hooks.slack.com/fallback"


async def test_alert_slack_can_deliver_through_relay_without_webhook(monkeypatch):
    monkeypatch.setenv("CODEX_TRIAGE_RELAY_URL", "https://triage.internal")
    monkeypatch.setenv("CODEX_TRIAGE_RELAY_TOKEN", "relay-secret")
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    with patch(
        "serving.observability.alerts._post_to_triage",
        new=AsyncMock(return_value=True),
    ) as mock_triage:
        sent = await alert_slack(AlertSeverity.WARN, "Latency high", {"p95_ms": 70_000})

    assert sent is True
    mock_triage.assert_awaited_once()


async def test_failed_delivery_does_not_consume_cooldown(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/fallback")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(side_effect=[False, True]),
    ) as mock_slack:
        first = await alert_slack(AlertSeverity.ERROR, "Provider failed", {}, dedupe_key="K")
        second = await alert_slack(AlertSeverity.ERROR, "Provider failed", {}, dedupe_key="K")

    assert first is False
    assert second is True
    assert mock_slack.await_count == 2


async def test_alert_slack_dedupes_within_cooldown(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(return_value=True),
    ) as mock_post:
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
    assert mock_post.await_count == 1


async def test_alert_slack_fires_again_after_cooldown(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    fake_now = [1000.0]

    def now():
        return fake_now[0]

    with (
        patch(
            "serving.observability.alerts._post_to_slack",
            new=AsyncMock(return_value=True),
        ) as mock_post,
        patch("serving.observability.alerts._monotonic", new=now),
    ):
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
        fake_now[0] += 61
        await alert_slack(AlertSeverity.WARN, "t", {}, dedupe_key="K", cooldown_sec=60)
    assert mock_post.await_count == 2


async def test_alert_slack_swallows_post_errors(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        # must not raise
        await alert_slack(AlertSeverity.ERROR, "t", {})


def test_server_info_has_expected_keys():
    info = server_info()
    assert set(info) >= {
        "hostname",
        "fqdn",
        "ip",
        "platform",
        "base_url",
        "environment",
    }
    assert info["hostname"]
    assert info["platform"]


def test_unconfigured_default_base_url_is_not_rendered(monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("base_url", raising=False)

    url, explicit = _base_url()
    assert (url, explicit) == ("", False)

    message = _format_message(AlertSeverity.ERROR, "Boom", {})
    assert "• *Base URL:* https://freeinference.org" not in message


def test_explicit_base_url_is_rendered(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://staging.freeinference.org")

    url, explicit = _base_url()
    assert (url, explicit) == ("https://staging.freeinference.org", True)

    message = _format_message(AlertSeverity.ERROR, "Boom", {})
    assert "• *Base URL:* https://staging.freeinference.org" in message


@pytest.mark.parametrize(
    "env_overrides, base_url, explicit, expected",
    [
        # Explicit DEPLOYMENT_ENV/ENVIRONMENT overrides always win (and are stripped).
        ({"DEPLOYMENT_ENV": "qa"}, "https://freeinference.org", True, "qa"),
        ({"ENVIRONMENT": "  canary\n"}, "https://staging.freeinference.org", True, "canary"),
        # Host-based inference (urlparse, so path segments don't misclassify).
        ({}, "https://staging.freeinference.org", True, "staging"),
        ({}, "https://freeinference.org", True, "production"),
        ({}, "http://localhost:8000", True, "local"),
        ({}, "http://127.0.0.1:8080/staging", True, "local"),
        ({}, "https://example.com", True, "unknown"),
        # Built-in default URL with no explicit config => treat as local, not prod.
        ({}, "https://freeinference.org", False, "local"),
        # Malformed URL (unclosed IPv6 literal) must not raise.
        ({}, "http://[::1", True, "unknown"),
    ],
)
def test_detect_environment(monkeypatch, env_overrides, base_url, explicit, expected):
    monkeypatch.delenv("DEPLOYMENT_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
    assert _detect_environment(base_url, explicit=explicit) == expected


def test_format_message_includes_server_block():
    message = _format_message(AlertSeverity.ERROR, "Boom", {"provider": "openai"})
    # Per-alert context (upstream provider) is still rendered.
    assert "• *Provider:* openai" in message
    # Gateway server identity is appended.
    assert "*Server*" in message
    assert "• *Host:*" in message
    info = server_info()
    assert info["hostname"] in message
