"""Test that circuit-breaker CLOSED→OPEN transition fires a Slack alert."""

import asyncio
from unittest.mock import AsyncMock, patch

from routing.routers import _CircuitBreaker, _CircuitState


async def test_circuit_open_fires_alert(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with patch("routing.routers.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")
        assert cb.state == _CircuitState.OPEN
        # alert_slack is fired via asyncio.ensure_future — let pending tasks run
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()


async def test_circuit_open_alert_only_on_first_transition(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with patch("routing.routers.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")  # CLOSED → OPEN
        cb.on_failure(reason="upstream_500")  # OPEN → OPEN (no alert)
        await asyncio.sleep(0)
        assert mock_alert.await_count == 1
