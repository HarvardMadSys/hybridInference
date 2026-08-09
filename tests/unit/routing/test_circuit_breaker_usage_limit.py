"""Circuit-breaker alert suppression for subscription usage-limit outages.

A provider that has spent its subscription window re-trips the breaker on every
half-open probe until the window resets. These tests assert the breaker pages
once per outage and stays quiet until the parsed reset time (see
``routing.usage_limit``), then re-arms on recovery.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
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


def _pages_sent(mock_alert):
    """Count delivered *firing* edges, ignoring the recovery (breached=False) ones.

    ``on_success`` reports its own resolution through the same call, so a raw
    await count conflates "paged twice" with "paged once, then recovered".
    """
    return sum(1 for call in mock_alert.await_args_list if call.kwargs.get("breached") is True)


async def test_usage_limit_alerts_once_then_suppresses_retrips(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)  # CLOSED -> OPEN
        assert cb.state == _CircuitState.OPEN
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()
        assert cb._alert_suppressed_until > 0.0
        context = mock_alert.await_args.kwargs["context"]()
        assert "quota_reset_at" in context

        # A half-open probe fails again with the same usage-limit error: the
        # breaker re-opens but must not page a second time.
        cb.state = _CircuitState.HALF_OPEN
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        assert cb.state == _CircuitState.OPEN
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()


async def test_active_mute_suppresses_non_usage_limit_retrip(monkeypatch):
    # Once an endpoint is muted for a usage-limit outage, a half-open re-trip that
    # surfaces as a different error — e.g. KeyPoolExhausted once the key's
    # 429-backoff exceeds the circuit cooldown, which carries no usage marker —
    # must stay muted rather than resurrect the storm.
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)  # trip + page
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()
        assert cb._alert_suppressed_until > 0.0

        # Half-open re-trip fails locally with a non-usage-limit error.
        cb.state = _CircuitState.HALF_OPEN
        cb.on_failure(reason="KeyPoolExhausted", detail="KeyPoolExhausted: all keys cooling down")
        assert cb.state == _CircuitState.OPEN
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()  # still just the one page


async def test_usage_limit_realerts_after_reset(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()

        # Simulate the reset window elapsing, then a fresh outage. Both clocks
        # have to elapse: the outage's mute and the floor under plan-usage pages.
        cb._alert_suppressed_until = 0.0
        cb._usage_limit_alerted_at = None
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
    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock(return_value=False)):
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        assert cb._alert_suppressed_until == 0.0
        assert cb._alert_in_flight_generation is None


async def test_recovery_during_delivery_does_not_restore_stale_mute(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()):
        # Trip schedules the page but it has not run yet (no await), so the
        # endpoint can recover while the page is still "in flight".
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_success()
        await _drain_alert_tasks()  # page now delivers, but for a stale generation
        # The recovered endpoint must not be re-muted by the late page.
        assert cb._alert_suppressed_until == 0.0
        assert cb._alert_in_flight_generation is None
        assert cb.state == _CircuitState.CLOSED


async def test_stale_in_flight_does_not_block_new_outage_after_recovery(monkeypatch):
    # If the endpoint recovers while a page is still sending and then trips again,
    # the stale (old-generation) in-flight guard must not suppress the new
    # outage's firing edge. The guard is generation-scoped, so recovery frees it.
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()):
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)  # outage 1 page in flight
        assert cb._alert_in_flight_generation == 0

        # Recovery bumps the generation while outage 1's page is still "sending".
        cb.on_success()
        assert cb._recovery_generation == 1

        # A fresh outage trips before that page finished — it must page, not be
        # blocked by the stale gen-0 guard, and claim the guard for gen 1.
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="chat_exception", detail=_WEEKLY_DETAIL)
        assert cb.state == _CircuitState.OPEN
        assert cb._alert_in_flight_generation == 1
        await _drain_alert_tasks()


async def test_non_usage_limit_failure_is_not_suppressed(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="openai:api.openai.com:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", detail="HTTP 502 bad gateway")
        cb.on_failure(reason="stream_exception", detail="HTTP 502 bad gateway")
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()
        # No usage-limit -> no suppression deadline, no reset context field.
        assert cb._alert_suppressed_until == 0.0
        assert "quota_reset_at" not in mock_alert.await_args.kwargs["context"]()


async def test_recovery_clears_suppression(monkeypatch):
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="zai:api.z.ai:443")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()):
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
        patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()),
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


async def test_registry_derives_detail_from_exc_for_hedged_failures(monkeypatch):
    # Hedged failure paths call record_failure(exc=...) with no `detail`; the
    # registry must derive it from the exception so usage-limit suppression still
    # engages for hedged endpoints (not just the router path that passes `detail`).
    _trip_env(monkeypatch)
    from routing.endpoint_health import EndpointHealthRegistry

    class _UsageLimitError(Exception):
        status_code = 429  # a usage-limit 429 trips the breaker (not a client error)

    reg = EndpointHealthRegistry()
    endpoint = "zai:api.z.ai:443"
    exc = _UsageLimitError(_WEEKLY_DETAIL)

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        reg.record_failure(endpoint, reason="UsageLimitError", exc=exc)
        reg.record_failure(endpoint, reason="UsageLimitError", exc=exc)  # trips
        await _drain_alert_tasks()
        mock_alert.assert_awaited_once()
        assert reg._circuits[endpoint]._alert_suppressed_until > 0.0
        assert "quota_reset_at" in mock_alert.await_args.kwargs["context"]()


async def test_recovery_does_not_re_arm_plan_usage_page_within_min_gap(monkeypatch):
    # The key-pool case: a plan exhaustion behind a pooled key can be "recovered"
    # by a sibling key that still has quota, which clears the outage mute. The
    # plan-usage page must still respect its 4h floor, or the alert re-fires on
    # every streak for the rest of the window.
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="minimax-m3:minimax-api")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        assert _pages_sent(mock_alert) == 1
        alerted_at = cb._usage_limit_alerted_at
        assert alerted_at is not None

        # A sibling key serves a request: the outage mute is cleared, but the
        # floor under plan-usage pages survives it.
        cb.on_success()
        assert cb._alert_suppressed_until == 0.0
        assert cb._usage_limit_alerted_at == alerted_at

        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        assert cb.state == _CircuitState.OPEN
        await _drain_alert_tasks()
        assert _pages_sent(mock_alert) == 1  # still just the one page


async def test_plan_usage_gap_does_not_mute_an_unrelated_outage(monkeypatch):
    # The floor caps *plan-usage* pages only. An endpoint that breaks for some
    # other reason inside the window is a different incident and must page.
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="minimax-m3:minimax-api")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        assert _pages_sent(mock_alert) == 1

        # Recovery, then a genuine failure that is not a usage limit.
        cb.on_success()
        cb.on_failure(reason="stream_exception", detail="HTTP 502 bad gateway")
        cb.on_failure(reason="stream_exception", detail="HTTP 502 bad gateway")
        await _drain_alert_tasks()
        assert _pages_sent(mock_alert) == 2


async def test_undelivered_page_does_not_start_the_min_gap(monkeypatch):
    # The floor is evidence-based like the mute: a page that never went out must
    # not buy four hours of silence.
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="minimax-m3:minimax-api")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock(return_value=False)):
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        assert cb._usage_limit_alerted_at is None


async def test_unspecified_window_reports_mute_deadline_not_a_guessed_reset(monkeypatch):
    # MiniMax's plan error names no reset, so the card must not invent a
    # quota_reset_at — it reports when the page can repeat instead.
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="minimax-m3:minimax-api")
    detail = (
        '{"type":"error","error":{"type":"rate_limit_error","message":'
        '"Token Plan usage limit reached: Upgrade your Token Plan or purchase '
        'Credits for more usage. (2056)","http_code":"429"}}'
    )

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", detail=detail)
        cb.on_failure(reason="stream_exception", detail=detail)
        await _drain_alert_tasks()
        context = mock_alert.await_args.kwargs["context"]()
        assert "quota_reset_at" not in context
        assert "alert_muted_until" in context
        # Muted for the full floor, not the old one-hour guess.
        muted_until = datetime.fromisoformat(context["alert_muted_until"])
        assert muted_until - datetime.now(timezone.utc) > timedelta(hours=3, minutes=55)


async def test_held_back_plan_page_still_mutes_the_outage(monkeypatch):
    # Holding a plan-usage page back must not leave the endpoint un-muted. The
    # same exhaustion re-trips in other shapes — once every pooled key is in its
    # 429 backoff, KeyPool.acquire raises KeyPoolExhausted before a request is
    # sent, and that text carries no usage marker — so those probes would escape
    # both gates and page at the 300s alert cooldown for the rest of the window.
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="minimax-m3:minimax-api")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()) as mock_alert:
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        assert _pages_sent(mock_alert) == 1

        # A sibling key serves a request, clearing the outage mute.
        cb.on_success()
        assert cb._alert_suppressed_until == 0.0

        # The plan error re-trips: rate-limited away, but it must re-arm the mute.
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()
        assert _pages_sent(mock_alert) == 1
        assert cb._alert_suppressed_until > 0.0

        # Now the pool is fully muted and every probe fails with a shape that
        # carries no usage marker. The re-armed mute has to hold them.
        for _ in range(6):
            cb.state = _CircuitState.HALF_OPEN
            cb.on_failure(
                reason="KeyPoolExhausted",
                detail="KeyPoolExhausted: All 2 keys for provider 'minimax' are muted",
            )
            await _drain_alert_tasks()
        assert _pages_sent(mock_alert) == 1


async def test_min_gap_clock_is_not_wall_clock(monkeypatch):
    # The gap measures elapsed time only, so it must be immune to an NTP step: a
    # wall clock jumping backwards must not extend the mute on plan-usage pages.
    _trip_env(monkeypatch)
    cb = _CircuitBreaker(provider="minimax-m3:minimax-api")

    with patch("routing.endpoint_health.alert_on_transition", new=AsyncMock()):
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        cb.on_failure(reason="stream_exception", detail=_WEEKLY_DETAIL)
        await _drain_alert_tasks()

    # Comparable to time.monotonic(), never to a wall-clock epoch: a monotonic
    # instant on a long-lived host is far smaller than an epoch timestamp.
    assert cb._usage_limit_alerted_at is not None
    assert cb._usage_limit_alerted_at < datetime.now(timezone.utc).timestamp() / 2
