"""Tests for AlertEngine and individual rule classes."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from serving.observability.alert_config import AlertConfig
from serving.observability.alert_rules import AlertEngine
from serving.observability.log_handler import AlertingLogHandler


async def test_engine_starts_and_stops_cleanly():
    handler = AlertingLogHandler(maxsize=10)
    cfg = AlertConfig()
    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    await engine.start()
    assert engine.is_running()
    await engine.stop()
    assert not engine.is_running()


def _fake_record(
    status_code: int,
    provider: str = "openai",
    model: str = "gpt-4",
    duration_ms: int = 100,
    path: str | None = None,
) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="serving.servers.middleware.request_log",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="http_request",
        args=None,
        exc_info=None,
    )
    rec.status_code = status_code
    rec.provider = provider
    rec.model = model
    rec.duration_ms = duration_ms
    rec.path = path
    return rec


def _fake_event(event: str, **fields) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="serving.servers.event",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg=event,
        args=None,
        exc_info=None,
    )
    rec.event = event
    for k, v in fields.items():
        setattr(rec, k, v)
    return rec


async def _drain_until(_handler: AlertingLogHandler, mock_alert, timeout_iters: int = 100):
    for _ in range(timeout_iters):
        if mock_alert.await_count > 0:
            break
        await asyncio.sleep(0.01)


async def test_failed_request_rate_fires_on_threshold(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.rules.failed_request_rate.window_sec = 60
    cfg.rules.failed_request_rate.threshold_pct = 5.0
    cfg.rules.failed_request_rate.min_samples = 10
    cfg.rules.failed_request_rate.cooldown_sec = 1
    # Disable other rules to keep the test focused
    cfg.rules.fivexx_rate.enabled = False
    cfg.rules.p95_latency_per_provider.enabled = False
    cfg.rules.auth_failure_spike.enabled = False
    cfg.rules.concurrency_exhausted.enabled = False

    handler = AlertingLogHandler(maxsize=1000)
    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await engine.start()
        try:
            for _ in range(10):
                handler.queue.put_nowait(_fake_record(200, path="/v1/messages"))
            for _ in range(2):
                handler.queue.put_nowait(
                    _fake_record(500, provider="anthropic", path="/v1/messages")
                )
            await _drain_until(handler, mock_alert)
            assert mock_alert.await_count >= 1
            ctx = mock_alert.await_args.args[2]
            assert ctx["top_status_codes"] == "500 (2)"
            assert ctx["top_paths"] == "/v1/messages (2)"
            assert ctx["top_providers"] == "anthropic (2)"
        finally:
            await engine.stop()


async def test_failed_request_rate_ignores_401(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.rules.failed_request_rate.window_sec = 60
    cfg.rules.failed_request_rate.threshold_pct = 5.0
    cfg.rules.failed_request_rate.min_samples = 10
    cfg.rules.failed_request_rate.cooldown_sec = 1
    cfg.rules.fivexx_rate.enabled = False
    cfg.rules.p95_latency_per_provider.enabled = False
    cfg.rules.auth_failure_spike.enabled = False
    cfg.rules.concurrency_exhausted.enabled = False

    handler = AlertingLogHandler(maxsize=1000)
    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await engine.start()
        try:
            # 19 OK + 3 401s = 13.6% would fire on the old >=400 predicate;
            # 401 is now ignored so no alert.
            for _ in range(19):
                handler.queue.put_nowait(_fake_record(200, path="/v1/messages"))
            for _ in range(3):
                handler.queue.put_nowait(_fake_record(401, path="/admin/recent-requests"))
            for _ in range(20):
                await asyncio.sleep(0.01)
            assert mock_alert.await_count == 0
        finally:
            await engine.stop()


async def test_fivexx_rate_fires_on_threshold(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.rules.fivexx_rate.window_sec = 60
    cfg.rules.fivexx_rate.threshold_pct = 2.0
    cfg.rules.fivexx_rate.min_samples = 10
    cfg.rules.fivexx_rate.cooldown_sec = 1
    cfg.rules.failed_request_rate.enabled = False
    cfg.rules.p95_latency_per_provider.enabled = False
    cfg.rules.auth_failure_spike.enabled = False
    cfg.rules.concurrency_exhausted.enabled = False

    handler = AlertingLogHandler(maxsize=1000)
    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await engine.start()
        try:
            # 50 records, 2 of them 5xx => 4% > 2% threshold
            for _ in range(48):
                handler.queue.put_nowait(_fake_record(200))
            for _ in range(2):
                handler.queue.put_nowait(_fake_record(503))
            await _drain_until(handler, mock_alert)
            assert mock_alert.await_count >= 1
            args, _kwargs = mock_alert.call_args
            # title should mention 5xx
            assert "5xx" in args[1]
        finally:
            await engine.stop()


async def test_p95_latency_per_provider_fires(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.rules.p95_latency_per_provider.window_sec = 60
    cfg.rules.p95_latency_per_provider.threshold_ms = 25000
    cfg.rules.p95_latency_per_provider.min_samples = 30
    cfg.rules.p95_latency_per_provider.cooldown_sec = 1
    cfg.rules.failed_request_rate.enabled = False
    cfg.rules.fivexx_rate.enabled = False
    cfg.rules.auth_failure_spike.enabled = False
    cfg.rules.concurrency_exhausted.enabled = False

    handler = AlertingLogHandler(maxsize=1000)
    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await engine.start()
        try:
            # 30 records ranging 1000..30000 ms; p95 should exceed 25000
            for ms in range(1000, 31000, 1000):
                handler.queue.put_nowait(_fake_record(200, provider="openai", duration_ms=ms))
            await _drain_until(handler, mock_alert)
            assert mock_alert.await_count >= 1
            args, _ = mock_alert.call_args
            # title contains the provider name
            assert "openai" in args[1]
        finally:
            await engine.stop()


async def test_p95_latency_per_provider_override(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.rules.p95_latency_per_provider.window_sec = 60
    cfg.rules.p95_latency_per_provider.threshold_ms = 25000
    cfg.rules.p95_latency_per_provider.min_samples = 30
    cfg.rules.p95_latency_per_provider.cooldown_sec = 1
    cfg.rules.p95_latency_per_provider.overrides = {"slow_one": {"threshold_ms": 60000}}
    cfg.rules.failed_request_rate.enabled = False
    cfg.rules.fivexx_rate.enabled = False
    cfg.rules.auth_failure_spike.enabled = False
    cfg.rules.concurrency_exhausted.enabled = False

    handler = AlertingLogHandler(maxsize=1000)
    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await engine.start()
        try:
            # 30 records up to 30000ms with override threshold 60000 → no alert
            for ms in range(1000, 31000, 1000):
                handler.queue.put_nowait(_fake_record(200, provider="slow_one", duration_ms=ms))
            # Allow draining time
            for _ in range(20):
                await asyncio.sleep(0.01)
            assert mock_alert.await_count == 0
        finally:
            await engine.stop()


async def test_auth_failure_spike_fires(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.rules.auth_failure_spike.window_sec = 60
    cfg.rules.auth_failure_spike.threshold_count = 50
    cfg.rules.auth_failure_spike.cooldown_sec = 1
    cfg.rules.failed_request_rate.enabled = False
    cfg.rules.fivexx_rate.enabled = False
    cfg.rules.p95_latency_per_provider.enabled = False
    cfg.rules.concurrency_exhausted.enabled = False

    handler = AlertingLogHandler(maxsize=1000)
    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await engine.start()
        try:
            for _ in range(60):
                handler.queue.put_nowait(
                    _fake_event(
                        "auth_failure",
                        remote_ip="1.2.3.4",
                        key_prefix="abc123",
                    )
                )
            await _drain_until(handler, mock_alert)
            assert mock_alert.await_count >= 1
        finally:
            await engine.stop()


async def test_concurrency_exhausted_fires(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.rules.concurrency_exhausted.window_sec = 300
    cfg.rules.concurrency_exhausted.threshold_count = 100
    cfg.rules.concurrency_exhausted.cooldown_sec = 1
    cfg.rules.failed_request_rate.enabled = False
    cfg.rules.fivexx_rate.enabled = False
    cfg.rules.p95_latency_per_provider.enabled = False
    cfg.rules.auth_failure_spike.enabled = False

    handler = AlertingLogHandler(maxsize=1000)
    engine = AlertEngine(
        handler=handler,
        config=cfg,
        scheduler=None,
        op_store=None,
        log_store=None,
    )

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await engine.start()
        try:
            for i in range(110):
                handler.queue.put_nowait(
                    _fake_event(
                        "concurrency_rejected",
                        user_id=f"user{i % 5}",
                        role="free",
                    )
                )
            await _drain_until(handler, mock_alert)
            assert mock_alert.await_count >= 1
        finally:
            await engine.stop()


class _FakeOpStore:
    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    async def query_users_over_daily_threshold(self, thresholds):
        self.calls += 1
        return self._rows


async def test_user_cost_overrun_job_fires(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alert_rules import UserCostOverrunJob
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.cost.user_overrun.thresholds_per_role = {"free": 5.0}
    cfg.cost.user_overrun.cooldown_sec = 1
    op_store = _FakeOpStore([("u1", "free", 7.50)])

    job = UserCostOverrunJob(cfg.cost.user_overrun, op_store)

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await job.run()
        assert mock_alert.await_count == 1
        args, _ = mock_alert.call_args
        assert "User cost overrun" in args[1]


class _FakeLogStore:
    def __init__(self, spends):
        self._spends = spends

    async def query_provider_hourly_spend(self, hour_iso):
        return self._spends


async def test_provider_hourly_spend_job_fires(monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alert_rules import ProviderHourlySpendJob
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()
    cfg.cost.provider_hourly_spend.budgets = {"openai": 100.0}
    cfg.cost.provider_hourly_spend.cooldown_sec = 1
    log_store = _FakeLogStore({"openai": 150.0, "anthropic": 25.0})

    job = ProviderHourlySpendJob(cfg.cost.provider_hourly_spend, log_store)

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await job.run()
        # only openai exceeded budget
        assert mock_alert.await_count == 1
        args, _ = mock_alert.call_args
        assert "openai" in args[1]
