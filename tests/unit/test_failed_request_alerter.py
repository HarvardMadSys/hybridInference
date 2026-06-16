"""Unit tests for the failed-request Slack alerter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── count_recent_failures SQL shape ──────────────────────────────────────


@pytest.mark.asyncio
async def test_count_recent_failures_query():
    """The failure-count SQL must filter on status_code >= 500 OR error IS NOT NULL."""
    from serving.admin.failed_request_alerter import (
        FAILED_REQUEST_COUNT_SQL,
        count_recent_failures,
    )

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=7)

    result = await count_recent_failures(pool, window_minutes=5)

    assert result == 7
    # Verify the constant-level predicate text.
    assert "status_code >= 500" in FAILED_REQUEST_COUNT_SQL
    assert "error IS NOT NULL" in FAILED_REQUEST_COUNT_SQL
    # "Model not found" (404 client errors) must be excluded so they never alert.
    assert "NOT ILIKE '%not found%'" in FAILED_REQUEST_COUNT_SQL
    # Verify the call was parameterized: SQL string + bind arg, not interpolated.
    pool.fetchval.assert_awaited_once_with(FAILED_REQUEST_COUNT_SQL, 5)


def test_both_queries_exclude_model_not_found():
    """Count and breakdown queries must both exclude 'model not found' errors."""
    from serving.admin.failed_request_alerter import (
        FAILED_REQUEST_BREAKDOWN_SQL,
        FAILED_REQUEST_COUNT_SQL,
    )

    # The exclusion lives on the error branch so genuine 5xx failures still count.
    assert "NOT ILIKE '%not found%'" in FAILED_REQUEST_COUNT_SQL
    assert "NOT ILIKE '%not found%'" in FAILED_REQUEST_BREAKDOWN_SQL


@pytest.mark.asyncio
async def test_count_recent_failures_rejects_nonpositive_window():
    """window_minutes <= 0 must raise ValueError before touching the pool."""
    from serving.admin.failed_request_alerter import count_recent_failures

    pool = MagicMock()
    pool.fetchval = AsyncMock()

    with pytest.raises(ValueError, match="window_minutes must be > 0"):
        await count_recent_failures(pool, window_minutes=0)

    pool.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_count_recent_failures_handles_null():
    """fetchval returning None must be coerced to 0, not blow up."""
    from serving.admin.failed_request_alerter import count_recent_failures

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)

    assert await count_recent_failures(pool, window_minutes=5) == 0


# ── Alerter behavior ─────────────────────────────────────────────────────


def _make_alerter(pool, *, threshold=20, window=5, cooldown=5, now_fn=None):
    from serving.admin.failed_request_alerter import FailedRequestAlerter

    return FailedRequestAlerter(
        pool=pool,
        webhook_url="https://hooks.slack.test/abc",
        threshold=threshold,
        window_minutes=window,
        cooldown_minutes=cooldown,
        now_fn=now_fn or (lambda: datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)),
    )


@pytest.mark.asyncio
async def test_alerter_no_fire_under_threshold():
    """Boundary: count == threshold (20) must NOT fire — strict greater-than."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=20)
    pool.fetchrow = AsyncMock(return_value=None)
    alerter = _make_alerter(pool, threshold=20)

    with patch(
        "serving.admin.failed_request_alerter.alert_slack",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_alert:
        await alerter.run_check()

    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_alerter_fires_over_threshold():
    """count == 21 (one over threshold of 20) must call alert_slack with expected context."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=21)
    pool.fetchrow = AsyncMock(return_value=None)
    alerter = _make_alerter(pool, threshold=20, window=5)

    with patch(
        "serving.admin.failed_request_alerter.alert_slack",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_alert:
        await alerter.run_check()

    mock_alert.assert_awaited_once()
    args, kwargs = mock_alert.await_args
    severity, title, context = args
    assert severity.value == "error"
    assert "Failed-request rate exceeded" in title
    assert context["count"] == 21
    assert context["window_minutes"] == 5
    assert context["threshold"] == 20
    assert kwargs["dedupe_key"] == "failed_request_rate_db"


@pytest.mark.asyncio
async def test_alerter_fires_includes_breakdown():
    """Breakdown fields (status_codes, providers, models, sample_error) are included in context."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=25)
    pool.fetchrow = AsyncMock(
        return_value={
            "status_codes": "500, 503",
            "providers": "openai",
            "models": "gpt-4o",
            "sample_error": "upstream timeout",
        }
    )
    alerter = _make_alerter(pool, threshold=20, window=5)

    with patch(
        "serving.admin.failed_request_alerter.alert_slack",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_alert:
        await alerter.run_check()

    args, _ = mock_alert.await_args
    _, _, context = args
    assert context["status_codes"] == "500, 503"
    assert context["providers"] == "openai"
    assert context["models"] == "gpt-4o"
    assert context["sample_error"] == "upstream timeout"


@pytest.mark.asyncio
async def test_alerter_breakdown_failure_does_not_block_alert():
    """If the breakdown query raises, the alert still fires with just count/window/threshold."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=25)
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db hiccup"))
    alerter = _make_alerter(pool, threshold=20, window=5)

    with patch(
        "serving.admin.failed_request_alerter.alert_slack",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_alert:
        await alerter.run_check()

    mock_alert.assert_awaited_once()
    args, _ = mock_alert.await_args
    _, _, context = args
    assert context["count"] == 25
    assert "status_codes" not in context


@pytest.mark.asyncio
async def test_alerter_fires_via_httpx_payload(monkeypatch):
    """End-to-end through alert_slack: httpx receives {"text": ...}."""
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.test/abc")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=25)
    pool.fetchrow = AsyncMock(return_value=None)
    alerter = _make_alerter(pool, threshold=20)

    fake_resp = MagicMock(status_code=200, text="ok")
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)

    class _ClientCtx:
        async def __aenter__(self_inner):
            return fake_client

        async def __aexit__(self_inner, *_a):
            return False

    # Patch httpx in the alerts module — that's where the new sink posts.
    with patch(
        "serving.observability.alerts.httpx.AsyncClient",
        return_value=_ClientCtx(),
    ):
        await alerter.run_check()

    fake_client.post.assert_awaited_once()
    _args, kwargs = fake_client.post.await_args
    body = kwargs["json"]["text"]
    assert "Failed-request rate exceeded" in body
    assert "25" in body


@pytest.mark.asyncio
async def test_alerter_cooldown_suppresses_repeat():
    """A second check inside the cooldown window must NOT post again."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=30)
    pool.fetchrow = AsyncMock(return_value=None)

    times = [
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 12, 2, 0, tzinfo=timezone.utc),  # +2 min, cooldown is 5
    ]

    def _clock():
        return times.pop(0)

    alerter = _make_alerter(pool, threshold=20, cooldown=5, now_fn=_clock)

    with patch(
        "serving.admin.failed_request_alerter.alert_slack",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_alert:
        await alerter.run_check()  # fires
        await alerter.run_check()  # suppressed by cooldown

    assert mock_alert.await_count == 1


@pytest.mark.asyncio
async def test_alerter_cooldown_expires_then_fires():
    """After the cooldown elapses, a second over-threshold check posts again."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=30)
    pool.fetchrow = AsyncMock(return_value=None)

    times = [
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 12, 6, 0, tzinfo=timezone.utc),  # +6 min, cooldown is 5
    ]

    def _clock():
        return times.pop(0)

    alerter = _make_alerter(pool, threshold=20, cooldown=5, now_fn=_clock)

    with patch(
        "serving.admin.failed_request_alerter.alert_slack",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_alert:
        await alerter.run_check()
        await alerter.run_check()

    assert mock_alert.await_count == 2


@pytest.mark.asyncio
async def test_alerter_failed_post_does_not_start_cooldown():
    """If the Slack POST fails, last_alert_at must remain unset so the next tick retries."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=30)
    pool.fetchrow = AsyncMock(return_value=None)

    times = [
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 12, 0, 30, tzinfo=timezone.utc),  # +30s, well inside cooldown
    ]

    def _clock():
        return times.pop(0)

    alerter = _make_alerter(pool, threshold=20, cooldown=5, now_fn=_clock)

    with patch(
        "serving.admin.failed_request_alerter.alert_slack",
        new_callable=AsyncMock,
        return_value=False,
    ) as mock_alert:
        await alerter.run_check()  # first POST fails
        await alerter.run_check()  # should retry — cooldown not started

    assert mock_alert.await_count == 2


# ── register_alerter_job ─────────────────────────────────────────────────


def _settings_stub(webhook_url=""):
    return MagicMock(
        slack_webhook_url=webhook_url,
        failed_request_alert_threshold=20,
        failed_request_alert_window_minutes=5,
        failed_request_alert_cooldown_minutes=5,
    )


def test_register_disabled_when_webhook_empty():
    """Empty SLACK_WEBHOOK_URL must skip registration entirely."""
    from serving.admin import failed_request_alerter

    fake_scheduler = MagicMock()
    pool = MagicMock()

    with patch.object(
        failed_request_alerter,
        "get_scheduler",
        return_value=fake_scheduler,
    ):
        failed_request_alerter.register_alerter_job(pool, _settings_stub(webhook_url=""))

    fake_scheduler.add_job.assert_not_called()


def test_register_disabled_when_webhook_whitespace():
    """Whitespace-only SLACK_WEBHOOK_URL must be treated as unset — no job registered."""
    from serving.admin import failed_request_alerter

    fake_scheduler = MagicMock()
    pool = MagicMock()

    with patch.object(
        failed_request_alerter,
        "get_scheduler",
        return_value=fake_scheduler,
    ):
        failed_request_alerter.register_alerter_job(pool, _settings_stub(webhook_url="   "))

    fake_scheduler.add_job.assert_not_called()


def test_register_adds_job_when_webhook_set():
    """A configured webhook URL must register an APScheduler interval job."""
    from serving.admin import failed_request_alerter

    fake_scheduler = MagicMock()
    pool = MagicMock()

    with patch.object(
        failed_request_alerter,
        "get_scheduler",
        return_value=fake_scheduler,
    ):
        failed_request_alerter.register_alerter_job(
            pool, _settings_stub(webhook_url="https://hooks.slack.test/xyz")
        )

    fake_scheduler.add_job.assert_called_once()
    _args, kwargs = fake_scheduler.add_job.call_args
    assert kwargs["id"] == "failed_request_alerter"
    assert kwargs["replace_existing"] is True
    assert kwargs["max_instances"] == 1


def test_register_no_scheduler_is_safe():
    """If get_scheduler returns None, registration logs and bails — no crash."""
    from serving.admin import failed_request_alerter

    pool = MagicMock()

    with patch.object(failed_request_alerter, "get_scheduler", return_value=None):
        # Must not raise.
        failed_request_alerter.register_alerter_job(
            pool, _settings_stub(webhook_url="https://hooks.slack.test/xyz")
        )


# ── post_slack_alert error swallowing ────────────────────────────────────


@pytest.mark.asyncio
async def test_slack_post_failure_is_swallowed():
    """httpx errors inside post_slack_alert must NOT propagate — returns False."""
    import httpx

    from serving.admin.failed_request_alerter import post_slack_alert

    class _BoomCtx:
        async def __aenter__(self_inner):
            raise httpx.ConnectError("boom")

        async def __aexit__(self_inner, *_a):
            return False

    with patch(
        "serving.admin.failed_request_alerter.httpx.AsyncClient",
        return_value=_BoomCtx(),
    ):
        ok = await post_slack_alert("https://hooks.slack.test/abc", "hi")

    assert ok is False


@pytest.mark.asyncio
async def test_slack_post_non_2xx_returns_false():
    """A non-2xx response must be treated as a failed post (False)."""
    from serving.admin.failed_request_alerter import post_slack_alert

    fake_resp = MagicMock(status_code=500, text="upstream broken")
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)

    class _ClientCtx:
        async def __aenter__(self_inner):
            return fake_client

        async def __aexit__(self_inner, *_a):
            return False

    with patch(
        "serving.admin.failed_request_alerter.httpx.AsyncClient",
        return_value=_ClientCtx(),
    ):
        ok = await post_slack_alert("https://hooks.slack.test/abc", "hi")

    assert ok is False


@pytest.mark.asyncio
async def test_alerter_swallows_query_exception():
    """If the failure-count query raises, run_check logs and returns — no crash."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("db down"))
    pool.fetchrow = AsyncMock(return_value=None)
    alerter = _make_alerter(pool)

    with patch(
        "serving.admin.failed_request_alerter.alert_slack",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_alert:
        await alerter.run_check()  # must not raise

    mock_alert.assert_not_awaited()


# Smoke check: timedelta math is what we expect — fences the cooldown semantics.
def test_cooldown_arithmetic_uses_timedelta():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert (base + timedelta(minutes=5)) - base == timedelta(minutes=5)
