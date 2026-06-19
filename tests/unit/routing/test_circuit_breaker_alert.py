"""Test that circuit-breaker CLOSED→OPEN transition fires a Slack alert."""

import asyncio
import gc
import logging
from unittest.mock import AsyncMock, patch

from routing.routers import BaseRouter, _CircuitBreaker, _CircuitState


def test_default_settings_keep_circuit_closed_until_third_failure(monkeypatch):
    monkeypatch.delenv("CIRCUIT_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("CIRCUIT_COOLDOWN_SECONDS", raising=False)
    monkeypatch.delenv("CIRCUIT_MIN_AVAILABILITY", raising=False)
    monkeypatch.delenv("ROUTER_HEALTH_EWMA_ALPHA", raising=False)

    router = BaseRouter()
    endpoint_id = "openai"

    router._on_failure(endpoint_id, reason="upstream_500")
    router._on_failure(endpoint_id, reason="upstream_500")
    assert router.get_provider_status()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED

    router._on_failure(endpoint_id, reason="upstream_500")
    assert router.get_provider_status()[endpoint_id]["circuit_state"] == _CircuitState.OPEN


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


async def test_circuit_open_alert_includes_upstream_error(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with patch("routing.routers.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", detail="HTTP 502 from upstream: bad gateway")
        cb.on_failure(reason="stream_exception", detail="HTTP 502 from upstream: bad gateway")
        assert cb.state == _CircuitState.OPEN
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        context = mock_alert.await_args.args[2]
        assert context["upstream_error"] == "HTTP 502 from upstream: bad gateway"
        assert context["reason"] == "stream_exception"


async def test_circuit_open_alert_omits_upstream_error_when_absent(monkeypatch):
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
        await asyncio.sleep(0)
        mock_alert.assert_awaited_once()
        context = mock_alert.await_args.args[2]
        assert "upstream_error" not in context


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


async def test_circuit_open_emits_structured_log(monkeypatch, caplog):
    """The CLOSED→OPEN transition logs a structured ``circuit_open`` record.

    The Slack alert is fire-and-forget and writes no log line, so this record
    is the only thing that makes the event visible in the application logs.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with (
        patch("routing.routers.alert_slack", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="routing.routers"),
    ):
        cb.on_failure(reason="stream_exception", detail="access_terminated_error")
        cb.on_failure(reason="stream_exception", detail="access_terminated_error")
        assert cb.state == _CircuitState.OPEN
        cb.on_success()
        assert cb.state == _CircuitState.CLOSED

    records = [r for r in caplog.records if r.getMessage() == "circuit_open"]
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.WARNING
    assert rec.event == "circuit_open"
    assert rec.provider == "openai"
    assert rec.reason == "stream_exception"
    assert rec.consecutive_failures == 2
    assert rec.upstream_error == "access_terminated_error"

    closed_records = [r for r in caplog.records if r.getMessage() == "circuit_closed"]
    assert len(closed_records) == 1
    closed_rec = closed_records[0]
    assert closed_rec.levelno == logging.INFO
    assert closed_rec.event == "circuit_closed"
    assert closed_rec.provider == "openai"
    assert closed_rec.duration_ms is not None


async def test_circuit_open_logs_once_per_transition(monkeypatch, caplog):
    """A circuit that is already OPEN does not re-emit the log on each failure."""
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cb = _CircuitBreaker(provider="openai")

    with (
        patch("routing.routers.alert_slack", new=AsyncMock()),
        caplog.at_level(logging.WARNING, logger="routing.routers"),
    ):
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")  # CLOSED → OPEN
        cb.on_failure(reason="upstream_500")  # OPEN → OPEN (no new log)

    opens = [r for r in caplog.records if r.getMessage() == "circuit_open"]
    assert len(opens) == 1


async def test_circuit_open_alert_survives_breaker_gc(monkeypatch):
    """The alert task must complete even if the breaker is GC'd immediately.

    Regression guard for the previous ``self._alert_task = ...`` pattern,
    which only kept the task alive for the breaker's lifetime; if the breaker
    was dropped before the task was scheduled, asyncio's weak-ref bookkeeping
    could let the GC cancel the alert mid-flight. The new implementation adds
    the task to a module-level ``_ALERT_TASKS`` set with ``add_done_callback``
    so it stays alive independently.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    with patch("routing.routers.alert_slack", new=AsyncMock()) as mock_alert:
        cb = _CircuitBreaker(provider="openai")
        cb.on_failure(reason="upstream_500")
        cb.on_failure(reason="upstream_500")
        assert cb.state == _CircuitState.OPEN
        # Drop all local references to the breaker and force collection.
        del cb
        gc.collect()
        # Yield to the loop so the scheduled alert task runs to completion.
        await asyncio.sleep(0.1)
        mock_alert.assert_awaited_once()
