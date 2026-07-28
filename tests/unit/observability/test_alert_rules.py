"""Tests for AlertEngine and individual rule classes."""

import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest

import serving.observability.alert_rules as alert_rules_module
from serving.observability.alert_config import AlertConfig
from serving.observability.alert_rules import AlertEngine, reset_transition_state
from serving.observability.log_handler import AlertingLogHandler


@pytest.fixture(autouse=True)
def _clean_transition_state():
    """Rules share one breach tracker, so an open breach would leak between tests.

    Without this a test that fires a key leaves it firing, and the next test
    using the same key sees a sustained breach rather than a new one and sends
    nothing — which is correct behaviour in production and a false failure here.
    """
    reset_transition_state()
    yield
    reset_transition_state()


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
    client_error_kind: str | None = None,
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
    rec.client_error_kind = client_error_kind
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
            # One message per breach, not one per record above the threshold:
            # the rule now sends on the crossing edge and stays quiet while the
            # breach persists, which is what lets the incident close later.
            assert mock_alert.await_count == 1
            ctx = mock_alert.await_args.args[2]
            # Counters therefore describe the moment the threshold was crossed.
            assert ctx["top_status_codes"] == "500 (1)"
            assert ctx["top_paths"] == "/v1/messages (1)"
            assert ctx["top_providers"] == "anthropic (1)"
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


async def test_failed_request_rate_ignores_model_not_found_404(monkeypatch):
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
            # 19 OK + 3 gateway model-not-found 404s = 13.6% would fire on the
            # old >=400 predicate; tagged model-not-found 404s are ignored
            # (user asked for an unknown model), so no alert.
            for _ in range(19):
                handler.queue.put_nowait(_fake_record(200, path="/v1/chat/completions"))
            for _ in range(3):
                handler.queue.put_nowait(
                    _fake_record(
                        404,
                        path="/v1/chat/completions",
                        client_error_kind="model_not_found",
                    )
                )
            for _ in range(20):
                await asyncio.sleep(0.01)
            assert mock_alert.await_count == 0
        finally:
            await engine.stop()


async def test_failed_request_rate_counts_upstream_404(monkeypatch):
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
            # Untagged 404s are upstream provider 404s (bad provider model id /
            # endpoint) — a genuine provider/config regression that must alert.
            # 19 OK + 3 untagged 404s = 13.6% > 5% threshold.
            for _ in range(19):
                handler.queue.put_nowait(_fake_record(200, path="/v1/chat/completions"))
            for _ in range(3):
                handler.queue.put_nowait(
                    _fake_record(404, provider="openai", path="/v1/chat/completions")
                )
            await _drain_until(handler, mock_alert)
            assert mock_alert.await_count >= 1
            ctx = mock_alert.await_args.args[2]
            assert "404" in ctx["top_status_codes"]
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


async def test_p95_latency_skips_records_without_provider(monkeypatch):
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
            for ms in range(1000, 32000, 1000):
                handler.queue.put_nowait(_fake_record(200, provider=None, duration_ms=ms))
            await _drain_until(handler, mock_alert)
            assert mock_alert.await_count == 0
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


async def test_concurrency_rejected_never_alerts(monkeypatch):
    """A user exhausting its quota/concurrency must never page Slack.

    Even a large spike of ``concurrency_rejected`` events (which would have
    tripped the removed ``concurrency_exhausted`` rule) must produce zero
    Slack alerts: per-user 429 rate limiting is expected, not a service fault.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    from serving.observability.alerts import reset_dedupe_state

    reset_dedupe_state()

    cfg = AlertConfig()

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
            # Wait for the engine to drain every queued record, then assert the
            # queue really is empty so a still-backlogged queue can't make the
            # "no alerts" check pass spuriously.
            for _ in range(100):
                if handler.queue.empty():
                    break
                await asyncio.sleep(0.01)
            assert handler.queue.empty(), "Queue was not fully drained"
            await asyncio.sleep(0.05)
            assert mock_alert.await_count == 0
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


# ---------------------------------------------------------------------------
# PendingPrefixCacheLeakRule
# ---------------------------------------------------------------------------


def _make_eviction_record(
    request_id: str = "r",
    age_sec: int = 400,
    *,
    reason: str = "ttl",
) -> logging.LogRecord:
    """Build a synthetic prefix-cache eviction log record for rule tests."""
    rec = logging.LogRecord(
        name="routing.routewise.prefix_cache_pending",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="routewise_prefix_cache_entry_evicted",
        args=None,
        exc_info=None,
    )
    rec.event = "routewise_prefix_cache_entry_evicted"
    rec.request_id = request_id
    rec.age_sec = age_sec
    rec.idle_sec = 301
    rec.reason = reason
    rec.pending_count = 42
    rec.capacity = 10_000
    return rec


def test_pending_prefix_cache_leak_config_parses() -> None:
    """The Pydantic config accepts the documented fields with documented defaults."""
    from serving.observability.alert_config import PendingPrefixCacheLeakConfig

    cfg = PendingPrefixCacheLeakConfig()
    assert cfg.enabled is True
    assert cfg.window_sec == 600
    assert cfg.threshold_count == 20
    assert cfg.cooldown_sec == 3600

    explicit = PendingPrefixCacheLeakConfig(
        enabled=False,
        window_sec=120,
        threshold_count=5,
        cooldown_sec=60,
    )
    assert explicit.enabled is False
    assert explicit.window_sec == 120
    assert explicit.threshold_count == 5
    assert explicit.cooldown_sec == 60


async def test_pending_prefix_cache_leak_rule_fires_above_threshold() -> None:
    """The rule fires once the in-window count exceeds threshold_count."""
    from serving.observability.alert_config import PendingPrefixCacheLeakConfig
    from serving.observability.alert_rules import PendingPrefixCacheLeakRule

    cfg = PendingPrefixCacheLeakConfig(
        enabled=True,
        window_sec=600,
        threshold_count=20,
        cooldown_sec=0,
    )
    rule = PendingPrefixCacheLeakRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new_callable=AsyncMock,
    ) as mock_alert:
        for i in range(20):
            await rule.on_record(_make_eviction_record(request_id=f"r{i}"))
        await rule.on_record(_make_eviction_record(request_id="cap", reason="size_cap"))

    assert mock_alert.await_count >= 1
    payload = mock_alert.call_args_list[0].args[2]
    assert payload["evicted_count"] == 21
    assert payload["window_sec"] == 600
    assert payload["ttl_count"] == 20
    assert payload["size_cap_count"] == 1
    assert payload["max_age_sec"] == 400
    assert payload["max_idle_sec"] == 301
    assert payload["max_pending_count"] == 42
    assert payload["capacity"] == 10_000


def test_pending_prefix_cache_eviction_fields_survive_log_formatters() -> None:
    """Operators retain eviction cause and pressure in JSON and plain logs."""
    from serving.utils.logging import JsonFormatter, PlainFormatter

    record = _make_eviction_record(reason="size_cap")
    payload = json.loads(JsonFormatter().format(record))
    plain = PlainFormatter("%(message)s").format(record)

    assert payload["reason"] == "size_cap"
    assert payload["age_sec"] == 400
    assert payload["idle_sec"] == 301
    assert payload["pending_count"] == 42
    assert payload["capacity"] == 10_000
    assert 'reason="size_cap"' in plain
    assert "pending_count=42" in plain
    assert "capacity=10000" in plain


async def test_pending_prefix_cache_leak_rule_does_not_fire_at_threshold() -> None:
    """At exactly threshold_count events the rule stays silent (strict >)."""
    from serving.observability.alert_config import PendingPrefixCacheLeakConfig
    from serving.observability.alert_rules import PendingPrefixCacheLeakRule

    cfg = PendingPrefixCacheLeakConfig(
        enabled=True,
        window_sec=600,
        threshold_count=20,
        cooldown_sec=0,
    )
    rule = PendingPrefixCacheLeakRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new_callable=AsyncMock,
    ) as mock_alert:
        for i in range(20):
            await rule.on_record(_make_eviction_record(request_id=f"r{i}"))

    assert mock_alert.await_count == 0


async def test_pending_prefix_cache_leak_rule_ignores_retired_event() -> None:
    """The retired pending-decision event must not advance the window."""
    from serving.observability.alert_config import PendingPrefixCacheLeakConfig
    from serving.observability.alert_rules import PendingPrefixCacheLeakRule

    cfg = PendingPrefixCacheLeakConfig(
        enabled=True,
        window_sec=600,
        threshold_count=1,
        cooldown_sec=0,
    )
    rule = PendingPrefixCacheLeakRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new_callable=AsyncMock,
    ) as mock_alert:
        rec = _make_eviction_record()
        rec.event = "routewise_decision_evicted"
        await rule.on_record(rec)
        await rule.on_record(rec)

    assert mock_alert.await_count == 0


async def test_pending_prefix_cache_leak_rule_disabled_does_not_fire() -> None:
    """When disabled the rule never invokes alert_slack."""
    from serving.observability.alert_config import PendingPrefixCacheLeakConfig
    from serving.observability.alert_rules import PendingPrefixCacheLeakRule

    cfg = PendingPrefixCacheLeakConfig(
        enabled=False,
        window_sec=600,
        threshold_count=0,
        cooldown_sec=0,
    )
    rule = PendingPrefixCacheLeakRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new_callable=AsyncMock,
    ) as mock_alert:
        for i in range(10):
            await rule.on_record(_make_eviction_record(request_id=f"r{i}"))

    assert mock_alert.await_count == 0


def test_alerts_yaml_loads_with_pending_prefix_cache_leak() -> None:
    """``config/alerts.yaml`` parses cleanly and the leak block matches defaults."""
    from pathlib import Path

    import yaml

    from serving.observability.alert_config import AlertConfig

    # tests/unit/observability/<file>.py -> repo root
    repo_root = Path(__file__).resolve().parents[3]
    yaml_path = repo_root / "config" / "alerts.yaml"
    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    assert "prefix_cache_pending_leak" in data["rules"]
    cfg = AlertConfig.model_validate(data)
    leak = cfg.rules.prefix_cache_pending_leak
    assert leak.enabled is True
    assert leak.window_sec == 600
    assert leak.threshold_count == 20
    assert leak.cooldown_sec == 3600


def test_legacy_pending_decisions_leak_config_migrates_for_one_release() -> None:
    """The retired key keeps custom thresholds while deployments migrate."""
    from serving.observability.alert_config import Rules

    rules = Rules.model_validate(
        {
            "pending_decisions_leak": {
                "enabled": False,
                "threshold_count": 999,
            }
        }
    )

    assert rules.prefix_cache_pending_leak.enabled is False
    assert rules.prefix_cache_pending_leak.threshold_count == 999


# ----------------------------------------------------------------------
# TrackedTaskFailureRateRule
# ----------------------------------------------------------------------


def _make_tracked_record(task_name: str, success: bool) -> logging.LogRecord:
    """Build a synthetic tracked_task_completed log record."""
    record = logging.LogRecord(
        name="serving.observability.tracked_tasks",
        level=logging.INFO if success else logging.WARNING,
        pathname=__file__,
        lineno=0,
        msg="tracked_task_completed",
        args=(),
        exc_info=None,
    )
    record.event = "tracked_task_completed"
    record.task_name = task_name
    record.success = success
    return record


def test_tracked_task_failure_rate_config_parses_yaml_defaults() -> None:
    """The Pydantic model accepts the spec's default values."""
    from serving.observability.alert_config import TrackedTaskFailureRateConfig

    cfg = TrackedTaskFailureRateConfig(
        enabled=True,
        window_sec=300,
        threshold_pct=5.0,
        min_samples=50,
        cooldown_sec=1800,
    )
    assert cfg.enabled is True
    assert cfg.window_sec == 300
    assert cfg.threshold_pct == 5.0
    assert cfg.min_samples == 50
    assert cfg.cooldown_sec == 1800


async def test_failure_rate_rule_fires_per_task_name() -> None:
    """A failing task_name fires; an unrelated task_name with low failures does not."""
    from serving.observability.alert_config import TrackedTaskFailureRateConfig
    from serving.observability.alert_rules import TrackedTaskFailureRateRule

    cfg = TrackedTaskFailureRateConfig(
        enabled=True,
        window_sec=600,
        threshold_pct=5.0,
        min_samples=50,
        cooldown_sec=0,
    )
    rule = TrackedTaskFailureRateRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack", new_callable=AsyncMock
    ) as mock_alert:
        # 100 records for request_log: 10 fail (10% > 5% threshold).
        for i in range(100):
            await rule.on_record(_make_tracked_record("request_log", success=(i >= 10)))
        # 100 records for cost_increment: 1 fails (1% < 5% threshold).
        for i in range(100):
            await rule.on_record(_make_tracked_record("cost_increment", success=(i != 0)))

    triggered_names = [call.args[2]["task_name"] for call in mock_alert.call_args_list]
    assert "request_log" in triggered_names
    assert "cost_increment" not in triggered_names


async def test_failure_rate_rule_skips_when_disabled() -> None:
    """A disabled rule never fires."""
    from serving.observability.alert_config import TrackedTaskFailureRateConfig
    from serving.observability.alert_rules import TrackedTaskFailureRateRule

    cfg = TrackedTaskFailureRateConfig(
        enabled=False,
        window_sec=300,
        threshold_pct=5.0,
        min_samples=1,
        cooldown_sec=0,
    )
    rule = TrackedTaskFailureRateRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack", new_callable=AsyncMock
    ) as mock_alert:
        for _ in range(10):
            await rule.on_record(_make_tracked_record("anything", success=False))
    assert mock_alert.await_count == 0


async def test_failure_rate_rule_skips_below_min_samples() -> None:
    """Below min_samples completions, the rule never fires."""
    from serving.observability.alert_config import TrackedTaskFailureRateConfig
    from serving.observability.alert_rules import TrackedTaskFailureRateRule

    cfg = TrackedTaskFailureRateConfig(
        enabled=True,
        window_sec=300,
        threshold_pct=5.0,
        min_samples=50,
        cooldown_sec=0,
    )
    rule = TrackedTaskFailureRateRule(cfg)

    with patch(
        "serving.observability.alert_rules.alert_slack", new_callable=AsyncMock
    ) as mock_alert:
        # 49 failures (below min_samples=50): no alert.
        for _ in range(49):
            await rule.on_record(_make_tracked_record("request_log", success=False))
    assert mock_alert.await_count == 0


def test_alerts_yaml_loads_with_tracked_task_failure_rate() -> None:
    """The committed alerts.yaml parses cleanly into AlertConfig with our defaults."""
    from pathlib import Path

    import yaml

    from serving.observability.alert_config import AlertConfig

    # tests/unit/observability -> repo root is parents[3].
    repo_root = Path(__file__).resolve().parents[3]
    yaml_path = repo_root / "config" / "alerts.yaml"
    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    cfg = AlertConfig(**data)
    rule_cfg = cfg.rules.tracked_task_failure_rate
    assert rule_cfg.enabled is True
    assert rule_cfg.window_sec == 300
    assert rule_cfg.threshold_pct == 5.0
    assert rule_cfg.min_samples == 50
    assert rule_cfg.cooldown_sec == 1800


@pytest.mark.asyncio
async def test_rate_rule_reports_recovery_once_the_breach_clears(monkeypatch):
    """The point of the wiring: a breach that ends must close its incident.

    Before this, the rule returned silently the moment the rate dropped back —
    the recovery was observable and discarded, which is why every gateway alert
    was fire-only and why feeding them to the control plane would have left
    incidents open forever.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    # Production waits out a settling period before closing so a metric sitting
    # on its threshold cannot flap; here it would just make the test sleep.
    monkeypatch.setattr(alert_rules_module._TRANSITIONS, "clear_after_sec", 0.0)

    cfg = AlertConfig()
    cfg.rules.failed_request_rate.enabled = True
    cfg.rules.failed_request_rate.min_samples = 4
    cfg.rules.failed_request_rate.threshold_pct = 40.0
    cfg.rules.failed_request_rate.cooldown_sec = 0
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
            # Breach: 3 of 4 failed.
            handler.queue.put_nowait(_fake_record(200))
            for _ in range(3):
                handler.queue.put_nowait(_fake_record(500, provider="anthropic"))
            await _drain_until(handler, mock_alert)
            assert mock_alert.await_count == 1
            assert mock_alert.await_args.kwargs.get("status", "firing") == "firing"

            # Healthy traffic pushes the window back under the threshold.
            for _ in range(20):
                handler.queue.put_nowait(_fake_record(200))
            for _ in range(100):
                if mock_alert.await_count > 1:
                    break
                await asyncio.sleep(0.02)

            assert mock_alert.await_count == 2
            assert mock_alert.await_args.kwargs["status"] == "resolved"
            # Same dedupe key, or the control plane would open a second
            # incident instead of closing the first.
            assert mock_alert.await_args.kwargs["dedupe_key"] == "failed_request_rate"
        finally:
            await engine.stop()


@pytest.mark.asyncio
async def test_sweep_closes_breaches_nothing_evaluates_any_more(monkeypatch):
    """Two breach classes never re-evaluate themselves and would stay open forever.

    A record-driven rule whose traffic stops entirely, and the budget jobs whose
    incident key embeds the day or hour so the previous period is never observed
    again. The scheduled sweep is what closes both.
    """
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setattr(alert_rules_module._TRANSITIONS, "stale_after_sec", 1.0)

    engine = AlertEngine(
        handler=AlertingLogHandler(maxsize=10),
        config=AlertConfig(),
        scheduler=None,
        op_store=None,
        log_store=None,
    )
    alert_rules_module._TRANSITIONS.observe(
        "cost_overrun:4711:2026-07-27", breached=True, now=time.time() - 10
    )

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(),
    ) as mock_alert:
        await engine._sweep_stale_breaches()

    assert mock_alert.await_count == 1
    assert mock_alert.await_args.kwargs["status"] == "resolved"
    assert mock_alert.await_args.kwargs["dedupe_key"] == "cost_overrun:4711:2026-07-27"
    assert not alert_rules_module._TRANSITIONS.is_firing("cost_overrun:4711:2026-07-27")


@pytest.mark.asyncio
async def test_sweep_keeps_closing_after_one_resolution_fails(monkeypatch):
    """One stuck resolution must not strand every other open incident."""
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://x")
    monkeypatch.setattr(alert_rules_module._TRANSITIONS, "stale_after_sec", 1.0)
    engine = AlertEngine(
        handler=AlertingLogHandler(maxsize=10),
        config=AlertConfig(),
        scheduler=None,
        op_store=None,
        log_store=None,
    )
    for key in ("a", "b"):
        alert_rules_module._TRANSITIONS.observe(key, breached=True, now=time.time() - 10)

    with patch(
        "serving.observability.alert_rules.alert_slack",
        new=AsyncMock(side_effect=[RuntimeError("slack down"), True]),
    ) as mock_alert:
        await engine._sweep_stale_breaches()

    assert mock_alert.await_count == 2
