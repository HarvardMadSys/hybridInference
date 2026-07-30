"""Circuit-breaker alert suppression for subscription usage-limit outages.

A provider that has spent its subscription window re-trips the breaker on every
half-open probe until the window resets. These tests assert the breaker pages
once per outage and stays quiet until the parsed reset time (see
``routing.usage_limit``), then re-arms on recovery.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from routing.endpoint_health import _ALERT_TASKS, _CircuitBreaker, _CircuitState

# "weekly usage limit" with no explicit timestamp -> reset ~7 days out,
# comfortably in the future regardless of when the test runs.
_WEEKLY_DETAIL = "you have reached your weekly usage limit, upgrade for higher limits"


def _trip_env(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.7")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()


async def _drain_alert_tasks():
    """Await the breaker's fire-and-forget page tasks so post-delivery state settles.

    The suppression deadline is committed inside ``_send_circuit_alert`` after the
    page is delivered, so tests that read ``_alert_suppressed_until`` must let that
    task finish first.
    """
    tasks = list(_ALERT_TASKS)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_usage_limit_alerts_once_then_suppresses_retrips(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)  # CLOSED -> OPEN
        assert cb.state == _CircuitState.OPEN
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()
        assert cb._alert_suppressed_until > 0.0
        context = mock_alert.await_args.args[2]
        assert "quota_reset_at" in context

        # A half-open probe fails again with the same usage-limit error: the
        # breaker re-opens but must not page a second time.
        cb.state = _CircuitState.HALF_OPEN
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        assert cb.state == _CircuitState.OPEN
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()


async def test_usage_limit_realerts_after_reset(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()

        # Simulate the reset window elapsing, then a fresh outage.
        cb._alert_suppressed_until = 0.0
        cb.state = _CircuitState.HALF_OPEN
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        assert mock_alert.await_count == 2
        assert cb._alert_suppressed_until > 0.0


async def test_undelivered_page_does_not_mute_the_outage(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    # A dropped page (relay/webhook failure, snooze, cooldown) returns False:
    # the outage must stay un-muted so the next probe re-pages.
    with patch("routing.endpoint_health.alert_slack", new=AsyncMock(return_value=False)):
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        assert cb._alert_suppressed_until == 0.0
        assert cb._alert_in_flight is False


async def test_non_usage_limit_failure_is_not_suppressed(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="openai:api.openai.com:443")

    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", detail="HTTP 502 bad gateway")
        cb.on_failure(reason="stream_exception", detail="HTTP 502 bad gateway")
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()
        # No usage-limit -> no suppression deadline, no reset context field.
        assert cb._alert_suppressed_until == 0.0
        assert "quota_reset_at" not in mock_alert.await_args.args[2]


async def test_recovery_clears_suppression(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()):
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        assert cb._alert_suppressed_until > 0.0
        cb.on_success()
        assert cb._alert_suppressed_until == 0.0
        assert cb.state == _CircuitState.CLOSED


async def test_suppressed_retrip_emits_structured_info_log(monkeypatch, caplog):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with (
        patch("routing.endpoint_health.alert_slack", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="routing.routers"),
    ):
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        cb.state = _CircuitState.HALF_OPEN
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)

    suppressed = [r for r in caplog.records if r.getMessage() == "circuit_open_alert_suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0].event == "circuit_open_alert_suppressed"
    assert suppressed[0].window == "weekly"
