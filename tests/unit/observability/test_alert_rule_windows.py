"""The alert rules' sliding windows: O(1) per record, and the same answers.

The rules used to copy and rescan their whole window for every record, and the
p95 rule sorted it, so an hour's window at 50 requests a second cost about as
much as the requests themselves. They now keep running tallies in step with
the window. These tests hold the rework to two promises:

* **The same answers.** ``_Legacy*`` below are the rescanning implementations
  as they stood, kept verbatim apart from where they send. Randomized record
  sequences -- gaps that empty the window, a clock that steps back, unhashable
  noise excluded -- go to both, and every evaluation must match: breach
  verdict, key, timing, and the whole card, top-N order included.
* **Work per record does not grow with the window.** Counted as Python lines
  run in ``alert_rules.py`` rather than timed, so a busy machine cannot make it
  flaky: a pass over the window shows up as lines in proportion to its size.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import random
import sys
import time
from typing import Any

import pytest

from serving.observability import alert_rules
from serving.observability.alert_config import (
    AlertConfig,
    AuthIpBlockedConfig,
    ClientErrorBurstConfig,
    CountRule,
    KeyPoolExhaustionBurstConfig,
    LatencyRule,
    PendingPrefixCacheLeakConfig,
    RateRule,
    RequestLogQueueSaturationConfig,
    StreamFailureRateConfig,
    TrackedTaskFailureRateConfig,
)
from serving.observability.alert_rules import (
    AlertEngine,
    AlertSeverity,
    AuthFailureSpikeRule,
    AuthIpBlockedRule,
    ClientErrorBurstRule,
    FailedRequestRateRule,
    FivexxRateRule,
    KeyPoolExhaustionBurstRule,
    P95LatencyRule,
    PendingPrefixCacheLeakRule,
    RequestLogQueueSaturationRule,
    StreamFailureRateRule,
    TrackedTaskFailureRateRule,
    _is_failed_request,
    _LatencyWindow,
    _Tally,
    _top_offenders,
)
from serving.observability.alerts import reset_transition_state
from serving.observability.log_handler import AlertingLogHandler
from serving.utils.context import MODEL_NOT_FOUND, MODEL_QUEUED

_STALE_WINDOW_FACTOR = alert_rules._STALE_WINDOW_FACTOR


@pytest.fixture(autouse=True)
def _clean_transition_state():
    reset_transition_state()
    yield
    reset_transition_state()


# --------------------------------------------------------------------------- #
# The rescanning implementations the rework replaced, as they stood.
# --------------------------------------------------------------------------- #


class _LegacyWindow:
    def __init__(self, window_sec: int) -> None:
        self.window_sec = window_sec
        self._items: collections.deque[tuple[float, dict]] = collections.deque()

    def add(self, ts: float, value: dict) -> None:
        self._items.append((ts, value))
        self._evict(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()

    def items(self, now: float) -> list[dict]:
        self._evict(now)
        return [v for _, v in self._items]


class _LegacyFailedRequestRate:
    name = "failed_request_rate"

    def __init__(self, cfg: RateRule, send) -> None:
        self._cfg = cfg
        self._window = _LegacyWindow(cfg.window_sec)
        self._send = send

    async def on_record(self, record: logging.LogRecord) -> None:
        if not self._cfg.enabled:
            return
        if record.name != alert_rules._REQUEST_LOG_LOGGER:
            return
        status = getattr(record, "status_code", None)
        if status is None:
            return
        now = time.time()
        self._window.add(
            now,
            {
                "status": int(status),
                "provider": getattr(record, "provider", None),
                "path": getattr(record, "path", None),
                "client_error_kind": getattr(record, "client_error_kind", None),
            },
        )
        items = self._window.items(now)
        if len(items) < self._cfg.min_samples:
            return
        failed = sum(1 for it in items if _is_failed_request(it))
        pct = (failed / len(items)) * 100.0

        def breach_context() -> dict[str, Any]:
            failed_items = [it for it in items if _is_failed_request(it)]
            status_counts: collections.Counter[int] = collections.Counter(
                it["status"] for it in failed_items
            )
            path_counts: collections.Counter[str] = collections.Counter(
                it["path"] for it in failed_items if it["path"]
            )
            prov_counts: collections.Counter[str] = collections.Counter(
                it["provider"] for it in failed_items if it["provider"]
            )
            top_s = ", ".join(f"{s} ({c})" for s, c in status_counts.most_common(3))
            top_paths = ", ".join(f"{p} ({c})" for p, c in path_counts.most_common(3))
            top_p = ", ".join(f"{p} ({c})" for p, c in prov_counts.most_common(3))
            return {
                "rate": (
                    f"{pct:.1f}% ({failed} of {len(items)} requests, last {self._cfg.window_sec}s)"
                ),
                "top_status_codes": top_s or "n/a",
                "top_paths": top_paths or "n/a",
                "top_providers": top_p or "n/a",
            }

        await self._send(
            key=self.name,
            breached=pct >= self._cfg.threshold_pct,
            severity=AlertSeverity.ERROR,
            title="Failed-request rate exceeded",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


class _LegacyFivexxRate:
    name = "fivexx_rate"

    def __init__(self, cfg: RateRule, send) -> None:
        self._cfg = cfg
        self._window = _LegacyWindow(cfg.window_sec)
        self._send = send

    async def on_record(self, record: logging.LogRecord) -> None:
        if not self._cfg.enabled:
            return
        if record.name != alert_rules._REQUEST_LOG_LOGGER:
            return
        status = getattr(record, "status_code", None)
        if status is None:
            return
        now = time.time()
        self._window.add(
            now,
            {"status": int(status), "provider": getattr(record, "provider", None)},
        )
        items = self._window.items(now)
        if len(items) < self._cfg.min_samples:
            return
        failed = sum(1 for it in items if it["status"] >= 500)
        pct = (failed / len(items)) * 100.0

        def breach_context() -> dict[str, Any]:
            prov_counts: collections.Counter[str] = collections.Counter(
                it["provider"] for it in items if it["status"] >= 500 and it["provider"]
            )
            status_counts: collections.Counter[int] = collections.Counter(
                it["status"] for it in items if it["status"] >= 500
            )
            top_p = ", ".join(f"{p} ({c})" for p, c in prov_counts.most_common(3))
            top_s = ", ".join(f"{s} ({c})" for s, c in status_counts.most_common(3))
            return {
                "rate": (
                    f"{pct:.1f}% ({failed} of {len(items)} requests, last {self._cfg.window_sec}s)"
                ),
                "top_providers": top_p or "n/a",
                "top_status_codes": top_s or "n/a",
            }

        await self._send(
            key=self.name,
            breached=pct >= self._cfg.threshold_pct,
            severity=AlertSeverity.ERROR,
            title="5xx rate exceeded",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


class _LegacyP95Latency:
    name = "p95_latency_per_provider"

    def __init__(self, cfg: LatencyRule, send) -> None:
        self._cfg = cfg
        self._windows: dict[str, _LegacyWindow] = {}
        self._send = send

    async def on_record(self, record: logging.LogRecord) -> None:
        if not self._cfg.enabled:
            return
        if record.name != alert_rules._REQUEST_LOG_LOGGER:
            return
        provider = getattr(record, "provider", None)
        if not provider:
            return
        duration_ms = getattr(record, "duration_ms", None)
        if duration_ms is None:
            return
        win = self._windows.setdefault(provider, _LegacyWindow(self._cfg.window_sec))
        now = time.time()
        win.add(now, {"duration_ms": int(duration_ms)})
        items = win.items(now)
        if len(items) < self._cfg.min_samples:
            return
        sorted_durations = sorted(it["duration_ms"] for it in items)
        idx = int(0.95 * (len(sorted_durations) - 1))
        p95 = sorted_durations[idx]
        threshold = self._cfg.overrides.get(provider, {}).get(
            "threshold_ms", self._cfg.threshold_ms
        )

        def breach_context() -> dict[str, Any]:
            p99_idx = int(0.99 * (len(sorted_durations) - 1))
            p99 = sorted_durations[p99_idx]
            return {
                "provider": provider,
                "p95_ms": p95,
                "p99_ms": p99,
                "samples": len(items),
                "window_sec": self._cfg.window_sec,
                "threshold_ms": threshold,
            }

        await self._send(
            key=f"p95_latency:{provider}",
            breached=p95 >= threshold,
            severity=AlertSeverity.WARN,
            title=f"p95 latency exceeded for provider {provider}",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


class _LegacyTrackedTaskFailureRate:
    name = "tracked_task_failure_rate"

    def __init__(self, cfg: TrackedTaskFailureRateConfig, send) -> None:
        self._cfg = cfg
        self._windows: dict[str, _LegacyWindow] = {}
        self._send = send

    async def on_record(self, record: logging.LogRecord) -> None:
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "tracked_task_completed":
            return
        task_name = getattr(record, "task_name", None) or "unknown"
        success = bool(getattr(record, "success", True))
        win = self._windows.setdefault(task_name, _LegacyWindow(self._cfg.window_sec))
        now = time.time()
        win.add(now, {"success": success})
        items = win.items(now)
        if len(items) < self._cfg.min_samples:
            return
        failed = sum(1 for it in items if not it["success"])
        pct = (failed / len(items)) * 100.0

        def breach_context() -> dict[str, Any]:
            return {
                "task_name": task_name,
                "rate": (
                    f"{pct:.1f}% ({failed} of {len(items)} tasks, last {self._cfg.window_sec}s)"
                ),
            }

        await self._send(
            key=f"tracked_task_failure:{task_name}",
            breached=pct >= self._cfg.threshold_pct,
            severity=AlertSeverity.ERROR,
            title=f"Tracked-task failure rate exceeded for {task_name}",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


class _LegacyClientErrorBurst:
    name = "client_error_burst"

    def __init__(self, cfg: ClientErrorBurstConfig, send) -> None:
        self._cfg = cfg
        self._window = _LegacyWindow(cfg.window_sec)
        self._send = send

    async def on_record(self, record: logging.LogRecord) -> None:
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "client_error_skip_breaker":
            return
        now = time.time()
        caller = getattr(record, "caller", None)
        self._window.add(
            now,
            {
                "endpoint_id": getattr(record, "endpoint_id", None),
                "status": getattr(record, "status", None),
                "caller": str(caller) if caller else "unknown",
            },
        )
        items = self._window.items(now)
        callers = len({it["caller"] for it in items})

        def breach_context() -> dict[str, Any]:
            endpoint_counts: collections.Counter[str] = collections.Counter(
                str(it["endpoint_id"]) for it in items if it["endpoint_id"]
            )
            status_counts: collections.Counter[str] = collections.Counter(
                str(it["status"]) for it in items if it["status"] is not None
            )
            return {
                "count": len(items),
                "window_sec": self._cfg.window_sec,
                "distinct_callers": callers,
                "top_endpoints": _top_offenders(endpoint_counts),
                "top_status_codes": _top_offenders(status_counts),
                "note": (
                    "these are client errors relayed from upstream; they bypass the "
                    "circuit breaker by design, so no circuit-open alert will follow. "
                    "A single caller replaying one malformed request can produce all "
                    "of them -- check the error detail in the logs for "
                    "client_error_skip_breaker before suspecting the endpoint"
                ),
            }

        await self._send(
            key="client_error_burst",
            breached=(
                len(items) > self._cfg.threshold_count and callers >= self._cfg.min_distinct_callers
            ),
            severity=AlertSeverity.WARN,
            title="Client-error burst relayed from upstream",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


class _LegacyStreamFailureRate:
    name = "stream_failure_rate"

    def __init__(self, cfg: StreamFailureRateConfig, send) -> None:
        self._cfg = cfg
        self._windows: dict[str, _LegacyWindow] = {}
        self._send = send

    async def on_record(self, record: logging.LogRecord) -> None:
        if not self._cfg.enabled:
            return
        if getattr(record, "event", None) != "stream_failed":
            return
        model = getattr(record, "model", None) or "unknown"
        win = self._windows.setdefault(str(model), _LegacyWindow(self._cfg.window_sec))
        now = time.time()
        win.add(now, {"error_type": getattr(record, "error_type", None)})
        items = win.items(now)

        def breach_context() -> dict[str, Any]:
            type_counts: collections.Counter[str] = collections.Counter(
                str(it["error_type"]) for it in items if it["error_type"]
            )
            return {
                "model": str(model),
                "count": len(items),
                "window_sec": self._cfg.window_sec,
                "top_error_types": _top_offenders(type_counts),
            }

        await self._send(
            key=f"stream_failure_rate:{model}",
            breached=len(items) > self._cfg.threshold_count,
            severity=AlertSeverity.ERROR,
            title=f"Streaming failures for model {model}",
            context=breach_context,
            cooldown_sec=self._cfg.cooldown_sec,
            stale_after=self._cfg.window_sec * _STALE_WINDOW_FACTOR,
            now=now,
        )


# --------------------------------------------------------------------------- #
# Records, clock and recorder.
# --------------------------------------------------------------------------- #


def _record(name: str = alert_rules._REQUEST_LOG_LOGGER, **fields: Any) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=fields.get("event", "http_request"),
        args=None,
        exc_info=None,
    )
    for key, value in fields.items():
        setattr(record, key, value)
    return record


def _outage(i: int) -> bool:
    """Alternate stretches of healthy traffic and outage, each longer than a window.

    So every sequence carries each rule across its threshold both ways, and the
    breach and the recovery are both compared.
    """
    return (i // 250) % 2 == 1


def _random_request(rng: random.Random, i: int) -> logging.LogRecord:
    roll = rng.random()
    if roll < 0.03:
        # Another logger: both implementations must ignore it.
        return _record(name="serving.something.else", status_code=500, provider="p")
    outage = _outage(i)
    fields: dict[str, Any] = {
        "provider": rng.choice([None, "", "openai", "anthropic", "zai", "zai"]),
        "path": rng.choice(
            [None, "", "/v1/chat/completions", "/v1/messages", "/v1/embeddings", "/admin/x"]
        ),
        "client_error_kind": rng.choice([None, None, None, MODEL_NOT_FOUND, MODEL_QUEUED]),
    }
    if rng.random() < 0.05:
        # Odd durations: none, negative, fractional, block edges, very long.
        fields["duration_ms"] = rng.choice([None, -5, 12.7, 1023, 1024, 2**21])
    elif outage and rng.random() < 0.5:
        fields["duration_ms"] = rng.choice(
            [25_000, 30_000, 64_000, 70_000, rng.randrange(5_000, 200_000)]
        )
    else:
        fields["duration_ms"] = rng.randrange(0, 999)
    if roll < 0.06:
        fields["status_code"] = None
    elif roll < 0.07:
        # Not a number: both implementations raise before counting it.
        fields["status_code"] = "oops"
    elif outage and rng.random() < 0.6:
        fields["status_code"] = rng.choice(
            [400, 401, 403, 404, 404, 408, 429, 500, 502, 503, 504, "500"]
        )
    else:
        fields["status_code"] = rng.choice([200, 200, 200, 201, 204, 200, 200, 200, 200, 404])
    return _record(**fields)


def _random_tracked_task(rng: random.Random, i: int) -> logging.LogRecord:
    if rng.random() < 0.03:
        return _record(name="x", event="something_else", task_name="request_log", success=False)
    fields: dict[str, Any] = {
        "event": "tracked_task_completed",
        "task_name": rng.choice([None, "", "request_log", "cost_increment", "key_last_used"]),
    }
    roll = rng.random()
    if roll < 0.9:
        fields["success"] = rng.random() < (0.5 if _outage(i) else 0.99)
    elif roll < 0.95:
        fields["success"] = rng.choice([0, 1, "", "yes"])
    return _record(name="serving.observability.tracked_tasks", **fields)


def _random_client_error(rng: random.Random, i: int) -> logging.LogRecord:
    if rng.random() < (0.03 if _outage(i) else 0.8):
        return _record(name="x", event="something_else", endpoint_id="e", status=400)
    return _record(
        name="routing.endpoint_health",
        event="client_error_skip_breaker",
        endpoint_id=rng.choice([None, "", 0, 7, "m:local-1", "m:zai-api", "m:zai-api"]),
        status=rng.choice([None, 400, 404, 422, 422, "400"]),
        caller=rng.choice([None, "", "k1", "k1", "k1", "k2", "k3"]),
    )


def _random_stream_failure(rng: random.Random, i: int) -> logging.LogRecord:
    if rng.random() < (0.03 if _outage(i) else 0.8):
        return _record(name="x", event="something_else", model="m1", error_type="E")
    return _record(
        name="serving.servers.routers.completions_stream",
        event="stream_failed",
        model=rng.choice([None, "", "m1", "m2", "m2"]),
        error_type=rng.choice([None, "", 3, "ValueError", "TimeoutError", "TimeoutError"]),
    )


class _Clock:
    """``time.time`` as the rules see it, stepped the way a real one misbehaves."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, window_sec: int) -> None:
        self.now = 1_700_000_000.0
        self.window_sec = window_sec
        monkeypatch.setattr(alert_rules.time, "time", lambda: self.now)

    def step(self, rng: random.Random) -> None:
        roll = rng.random()
        if roll < 0.05:
            return  # two records in the same instant
        if roll < 0.054:
            self.now += self.window_sec * rng.uniform(1.0, 2.5)  # the window empties
        elif roll < 0.074:
            self.now -= rng.uniform(0.0, 5.0)  # the clock steps back
        else:
            self.now += rng.expovariate(2.0)


def _recorder(calls: list[dict[str, Any]]):
    """Stand in for ``alert_on_transition``, reading the card on every evaluation.

    The real one reads it only for a breach; reading it every time compares
    more, and neither implementation's card may depend on being read.
    """

    async def record(**kwargs: Any) -> bool:
        context = kwargs.pop("context")
        calls.append({**kwargs, "context": context()})
        return False

    return record


async def _replay(new_rule, old_rule, records, clock: _Clock, rng, new_calls, old_calls) -> None:
    for record in records:
        clock.step(rng)
        outcomes = []
        for rule in (new_rule, old_rule):
            try:
                await rule.on_record(record)
                outcomes.append(None)
            except Exception as exc:
                outcomes.append(type(exc))
        assert outcomes[0] == outcomes[1]
        assert len(new_calls) == len(old_calls)
        if new_calls:
            assert new_calls[-1] == old_calls[-1]


# --------------------------------------------------------------------------- #
# The same answers as the rescan.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(8))
async def test_failed_request_rate_matches_the_rescan_it_replaced(seed, monkeypatch):
    rng = random.Random(seed)
    cfg = RateRule(
        window_sec=60,
        threshold_pct=rng.choice([5.0, 20.0, 33.3]),
        min_samples=rng.choice([1, 10, 40]),
        cooldown_sec=0,
    )
    new_calls: list[dict[str, Any]] = []
    old_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_rules, "alert_on_transition", _recorder(new_calls))
    clock = _Clock(monkeypatch, cfg.window_sec)
    records = [_random_request(rng, i) for i in range(1500)]
    await _replay(
        FailedRequestRateRule(cfg),
        _LegacyFailedRequestRate(cfg, _recorder(old_calls)),
        records,
        clock,
        rng,
        new_calls,
        old_calls,
    )
    assert any(call["breached"] for call in new_calls)
    assert any(not call["breached"] for call in new_calls)


@pytest.mark.parametrize("seed", range(8))
async def test_fivexx_rate_matches_the_rescan_it_replaced(seed, monkeypatch):
    rng = random.Random(seed)
    cfg = RateRule(
        window_sec=60,
        threshold_pct=rng.choice([5.0, 20.0, 25.0]),
        min_samples=rng.choice([1, 10, 40]),
        cooldown_sec=0,
    )
    new_calls: list[dict[str, Any]] = []
    old_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_rules, "alert_on_transition", _recorder(new_calls))
    clock = _Clock(monkeypatch, cfg.window_sec)
    records = [_random_request(rng, i) for i in range(1500)]
    await _replay(
        FivexxRateRule(cfg),
        _LegacyFivexxRate(cfg, _recorder(old_calls)),
        records,
        clock,
        rng,
        new_calls,
        old_calls,
    )
    assert any(call["breached"] for call in new_calls)
    assert any(not call["breached"] for call in new_calls)


@pytest.mark.parametrize("seed", range(8))
async def test_p95_latency_matches_the_sort_it_replaced(seed, monkeypatch):
    rng = random.Random(seed)
    cfg = LatencyRule(
        window_sec=60,
        threshold_ms=rng.choice([1000, 25_000, 30_000, 64_000]),
        min_samples=rng.choice([1, 5, 30]),
        cooldown_sec=0,
        overrides={"zai": {"threshold_ms": 5000}},
    )
    new_calls: list[dict[str, Any]] = []
    old_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_rules, "alert_on_transition", _recorder(new_calls))
    clock = _Clock(monkeypatch, cfg.window_sec)
    new_rule = P95LatencyRule(cfg)
    old_rule = _LegacyP95Latency(cfg, _recorder(old_calls))
    for phase in range(3):
        records = [_random_request(rng, i) for i in range(phase * 600, (phase + 1) * 600)]
        await _replay(new_rule, old_rule, records, clock, rng, new_calls, old_calls)
        # A threshold that changes under a populated window is recounted, not
        # carried over.
        cfg.threshold_ms = rng.choice([500, 2000, 30_000, 70_000])
    assert any(call["breached"] for call in new_calls)
    assert any(not call["breached"] for call in new_calls)


@pytest.mark.parametrize("seed", range(6))
async def test_tracked_task_failure_rate_matches_the_rescan_it_replaced(seed, monkeypatch):
    rng = random.Random(seed)
    cfg = TrackedTaskFailureRateConfig(
        window_sec=60,
        threshold_pct=rng.choice([5.0, 20.0, 30.0]),
        min_samples=rng.choice([1, 10, 30]),
        cooldown_sec=0,
    )
    new_calls: list[dict[str, Any]] = []
    old_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_rules, "alert_on_transition", _recorder(new_calls))
    clock = _Clock(monkeypatch, cfg.window_sec)
    records = [_random_tracked_task(rng, i) for i in range(1500)]
    await _replay(
        TrackedTaskFailureRateRule(cfg),
        _LegacyTrackedTaskFailureRate(cfg, _recorder(old_calls)),
        records,
        clock,
        rng,
        new_calls,
        old_calls,
    )
    assert any(call["breached"] for call in new_calls)
    assert any(not call["breached"] for call in new_calls)


@pytest.mark.parametrize("seed", range(6))
async def test_client_error_burst_matches_the_rescan_it_replaced(seed, monkeypatch):
    rng = random.Random(seed)
    cfg = ClientErrorBurstConfig(
        window_sec=60,
        threshold_count=rng.choice([5, 20, 40]),
        cooldown_sec=0,
        min_distinct_callers=rng.choice([1, 2, 3]),
    )
    new_calls: list[dict[str, Any]] = []
    old_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_rules, "alert_on_transition", _recorder(new_calls))
    clock = _Clock(monkeypatch, cfg.window_sec)
    records = [_random_client_error(rng, i) for i in range(1500)]
    await _replay(
        ClientErrorBurstRule(cfg),
        _LegacyClientErrorBurst(cfg, _recorder(old_calls)),
        records,
        clock,
        rng,
        new_calls,
        old_calls,
    )
    assert any(call["breached"] for call in new_calls)
    assert any(not call["breached"] for call in new_calls)


@pytest.mark.parametrize("seed", range(6))
async def test_stream_failure_rate_matches_the_rescan_it_replaced(seed, monkeypatch):
    rng = random.Random(seed)
    cfg = StreamFailureRateConfig(
        window_sec=60, threshold_count=rng.choice([3, 10, 20]), cooldown_sec=0
    )
    new_calls: list[dict[str, Any]] = []
    old_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_rules, "alert_on_transition", _recorder(new_calls))
    clock = _Clock(monkeypatch, cfg.window_sec)
    records = [_random_stream_failure(rng, i) for i in range(1500)]
    await _replay(
        StreamFailureRateRule(cfg),
        _LegacyStreamFailureRate(cfg, _recorder(old_calls)),
        records,
        clock,
        rng,
        new_calls,
        old_calls,
    )
    assert any(call["breached"] for call in new_calls)
    assert any(not call["breached"] for call in new_calls)


@pytest.mark.parametrize("seed", range(5))
def test_a_tally_ranks_exactly_as_a_counter_over_the_window(seed):
    """Same keys, counts and tie order as ``Counter(window).most_common``."""
    rng = random.Random(seed)
    tally = _Tally()
    window: collections.deque[tuple[str, Any]] = collections.deque()
    for arrival in range(3000):
        if window and rng.random() < 0.45:
            _key, token = window.popleft()
            tally.discard(token)
        else:
            # A fresh, equal string each time, as one per request would be.
            key = "".join(rng.choice("abcde") for _ in range(rng.choice([1, 1, 2])))
            window.append((key, tally.add(key, arrival)))
        rescan = collections.Counter(key for key, _token in window)
        assert len(tally) == len(rescan)
        for n in (1, 3, 40):
            assert tally.most_common(n) == rescan.most_common(n)
        assert _top_offenders(tally) == _top_offenders(rescan)


def test_a_tallys_samples_share_one_copy_of_their_key():
    tally = _Tally()
    first = "/v1/" + "x" * 2000
    tokens = [tally.add("/v1/" + "x" * 2000, arrival) for arrival in range(100)]
    assert all(token is tokens[0] for token in tokens)
    assert tally.add(first, 100) is tokens[0]


@pytest.mark.parametrize("seed", range(5))
def test_latency_window_selects_exactly_what_a_sort_selects(seed):
    rng = random.Random(seed)
    window = _LatencyWindow(window_sec=50)
    reference: collections.deque[tuple[float, int]] = collections.deque()
    now = 0.0
    for _ in range(2500):
        now += rng.expovariate(4.0)
        duration = rng.choice(
            [rng.randrange(-3, 3), rng.randrange(0, 5000), rng.randrange(0, 3_000_000), 1024]
        )
        samples = window.add(now, duration)
        reference.append((now, duration))
        while reference and reference[0][0] < now - 50:
            reference.popleft()
        assert samples == len(reference)
        ordered = sorted(duration for _ts, duration in reference)
        for k in {0, len(ordered) // 2, int(0.95 * (len(ordered) - 1)), len(ordered) - 1}:
            assert window.kth_smallest(k) == ordered[k]
        threshold = rng.choice([0, 1024, 2500, 100_000])
        assert window.at_or_above(threshold) == sum(1 for d in ordered if d >= threshold)


# --------------------------------------------------------------------------- #
# Work per record does not grow with the window.
# --------------------------------------------------------------------------- #


class _LinesRun:
    """Count the Python lines run in ``alert_rules.py`` while active.

    A deterministic stand-in for a timer: a pass over the window shows up as
    lines in proportion to its size, however busy the machine is.
    """

    def __init__(self) -> None:
        self.lines = 0
        self._filename = alert_rules.__file__

    def _local(self, frame, event, arg):
        if event == "line":
            self.lines += 1
        return self._local

    def _global(self, frame, event, arg):
        return self._local if frame.f_code.co_filename == self._filename else None

    def __enter__(self) -> _LinesRun:
        self._previous = sys.gettrace()
        sys.settrace(self._global)
        return self

    def __exit__(self, *exc_info: object) -> None:
        sys.settrace(self._previous)


#: Window lengths compared. Powers of two keep the record spacing exact, so at
#: steady state every record evicts exactly one.
_SMALL_WINDOW = 64
_LARGE_WINDOW = 4096
_WINDOW_SEC = 1024
_MEASURED = 24


def _request(
    status: int,
    *,
    provider: str = "openai",
    path: str = "/v1/chat/completions",
    duration_ms: int = 100,
) -> logging.LogRecord:
    return _record(status_code=status, provider=provider, path=path, duration_ms=duration_ms)


def _cycle(*records: logging.LogRecord):
    def make(i: int) -> logging.LogRecord:
        return records[i % len(records)]

    return make


_HEALTHY_TRAFFIC = _cycle(_request(200), _request(201), _request(200, provider="zai"))
_FAILING_TRAFFIC = _cycle(
    _request(200),
    _request(500, path="/a", duration_ms=40_000),
    _request(502, provider="zai", path="/b", duration_ms=9_000),
    _request(404, provider="", path="/c", duration_ms=70_000),
    _request(200, duration_ms=3_000),
    _request(503, path="/a", duration_ms=50_000),
)


def _event(event: str, **fields: Any):
    return _cycle(_record(name="x", event=event, **fields))


# Each case: a rule over a _WINDOW_SEC window, the records it sees, and whether
# they hold it in breach, so the card is built on every record too.
_SCALING_CASES = {
    "failed_request_rate healthy": (
        lambda: FailedRequestRateRule(RateRule(window_sec=_WINDOW_SEC, min_samples=1)),
        _HEALTHY_TRAFFIC,
        False,
    ),
    "failed_request_rate breached": (
        lambda: FailedRequestRateRule(RateRule(window_sec=_WINDOW_SEC, min_samples=1)),
        _FAILING_TRAFFIC,
        True,
    ),
    "fivexx_rate healthy": (
        lambda: FivexxRateRule(RateRule(window_sec=_WINDOW_SEC, min_samples=1)),
        _HEALTHY_TRAFFIC,
        False,
    ),
    "fivexx_rate breached": (
        lambda: FivexxRateRule(RateRule(window_sec=_WINDOW_SEC, min_samples=1)),
        _FAILING_TRAFFIC,
        True,
    ),
    "p95_latency healthy": (
        lambda: P95LatencyRule(LatencyRule(window_sec=_WINDOW_SEC, min_samples=1)),
        _HEALTHY_TRAFFIC,
        False,
    ),
    "p95_latency breached": (
        lambda: P95LatencyRule(
            LatencyRule(window_sec=_WINDOW_SEC, min_samples=1, threshold_ms=1000)
        ),
        _cycle(*(_request(200, duration_ms=d) for d in (900, 40_000, 9_000, 70_000, 3_000))),
        True,
    ),
    "tracked_task_failure_rate breached": (
        lambda: TrackedTaskFailureRateRule(
            TrackedTaskFailureRateConfig(window_sec=_WINDOW_SEC, min_samples=1)
        ),
        _cycle(
            _record(name="t", event="tracked_task_completed", task_name="t", success=False),
            _record(name="t", event="tracked_task_completed", task_name="t", success=True),
        ),
        True,
    ),
    "client_error_burst breached": (
        lambda: ClientErrorBurstRule(
            ClientErrorBurstConfig(window_sec=_WINDOW_SEC, threshold_count=1)
        ),
        _cycle(
            *(
                _record(name="x", event="client_error_skip_breaker", endpoint_id=e, status=s)
                for e, s in (("m:a", 400), ("m:b", 422), ("m:a", 404), ("m:c", 400))
            )
        ),
        True,
    ),
    "stream_failure_rate breached": (
        lambda: StreamFailureRateRule(
            StreamFailureRateConfig(window_sec=_WINDOW_SEC, threshold_count=1)
        ),
        _cycle(
            *(
                _record(name="x", event="stream_failed", model="m", error_type=t)
                for t in ("ValueError", "TimeoutError", "ValueError")
            )
        ),
        True,
    ),
    "key_pool_exhaustion_burst breached": (
        lambda: KeyPoolExhaustionBurstRule(
            KeyPoolExhaustionBurstConfig(window_sec=_WINDOW_SEC, threshold_count=1)
        ),
        _event("key_pool_exhausted_skip_breaker", endpoint_id="m:a"),
        True,
    ),
    "auth_ip_blocked breached": (
        lambda: AuthIpBlockedRule(AuthIpBlockedConfig(window_sec=_WINDOW_SEC)),
        _event("auth_ip_blocked", ip_bucket="203.0.113.7", block_seconds=60),
        True,
    ),
    # These three build their card from the window, but only for a breached
    # evaluation; a window short of the threshold must cost nothing extra.
    "auth_failure_spike healthy": (
        lambda: AuthFailureSpikeRule(
            CountRule(window_sec=_WINDOW_SEC, threshold_count=10 * _LARGE_WINDOW)
        ),
        _event("auth_failure", remote_ip="203.0.113.7", key_prefix="sk-x", reason="bad"),
        False,
    ),
    "prefix_cache_pending_leak healthy": (
        lambda: PendingPrefixCacheLeakRule(
            PendingPrefixCacheLeakConfig(window_sec=_WINDOW_SEC, threshold_count=10 * _LARGE_WINDOW)
        ),
        _event("routewise_prefix_cache_entry_evicted", reason="ttl", age_sec=1.0),
        False,
    ),
    "request_log_queue_saturation healthy": (
        lambda: RequestLogQueueSaturationRule(
            RequestLogQueueSaturationConfig(
                window_sec=_WINDOW_SEC, threshold_count=10 * _LARGE_WINDOW
            )
        ),
        _event("request_log_queue_saturated", dropped_total=1, queue_maxsize=1024),
        False,
    ),
}


async def _lines_per_record(monkeypatch, make_rule, make_record, window: int) -> tuple[float, set]:
    """Lines of ``alert_rules.py`` run per record once the window holds ``window``."""
    verdicts: set[bool] = set()

    async def evaluate(**kwargs: Any) -> bool:
        verdicts.add(kwargs["breached"])
        if kwargs["breached"]:
            kwargs["context"]()  # as alert_on_transition does
        return False

    monkeypatch.setattr(alert_rules, "alert_on_transition", evaluate)
    clock = [1_000_000.0]
    monkeypatch.setattr(alert_rules.time, "time", lambda: clock[0])
    spacing = _WINDOW_SEC / window
    rule = make_rule()
    for i in range(window + 8):
        clock[0] += spacing
        await rule.on_record(make_record(i))
    verdicts.clear()
    with _LinesRun() as run:
        for i in range(window + 8, window + 8 + _MEASURED):
            clock[0] += spacing
            await rule.on_record(make_record(i))
    return run.lines / _MEASURED, verdicts


@pytest.mark.parametrize("case", list(_SCALING_CASES))
async def test_work_per_record_does_not_grow_with_the_window(case, monkeypatch):
    make_rule, make_record, breached = _SCALING_CASES[case]
    small, small_verdicts = await _lines_per_record(
        monkeypatch, make_rule, make_record, _SMALL_WINDOW
    )
    large, large_verdicts = await _lines_per_record(
        monkeypatch, make_rule, make_record, _LARGE_WINDOW
    )
    # The scenario really exercised the path it names.
    assert small_verdicts == large_verdicts == {breached}
    # A rescan would make this 64 times larger; allow noise, not growth.
    assert large <= small * 1.25 + 2, (case, small, large)


# --------------------------------------------------------------------------- #
# Bounds on what a window holds.
# --------------------------------------------------------------------------- #


async def test_a_scan_of_random_paths_cannot_grow_the_path_tally(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_rules, "alert_on_transition", _recorder(calls))
    start = 1_000_000.0
    clock = [start]
    monkeypatch.setattr(alert_rules.time, "time", lambda: clock[0])
    rule = FailedRequestRateRule(RateRule(window_sec=3600, min_samples=1, threshold_pct=1.0))
    cap = alert_rules._MAX_TRACKED_PATHS

    # One failure a second: failures on a scan of distinct paths (a 5xx counts
    # whoever answered it), then a real outage.
    for i in range(cap + 100):
        clock[0] += 1
        await rule.on_record(_request(502, provider="", path=f"/scan/{i}"))
    for _ in range(5):
        clock[0] += 1
        await rule.on_record(_request(500, path="/v1/chat/completions"))

    assert len(rule._paths) == cap
    card = calls[-1]["context"]
    # The rate still counts every failure; only the path tally is capped, and
    # its line says how much it left out.
    assert card["rate"] == f"100.0% ({cap + 105} of {cap + 105} requests, last 3600s)"
    assert card["top_status_codes"] == f"502 ({cap + 100}), 500 (5)"
    assert (
        card["top_paths"] == "/scan/0 (1), /scan/1 (1), /scan/2 (1), +105 on other paths (capped)"
    )

    # The tracked paths age out first, while the failures they crowded out are
    # still in the window: the freed slots take new paths, and the line still
    # owns up to what it never counted.
    clock[0] = start + cap + 3600.5
    await rule.on_record(_request(500, path="/v1/messages"))
    assert len(rule._paths) == 1
    assert calls[-1]["context"]["top_paths"] == "/v1/messages (1), +105 on other paths (capped)"

    # And once everything has aged out, nothing is capped any more.
    clock[0] += 3601
    await rule.on_record(_request(500, path="/v1/chat/completions"))
    assert calls[-1]["context"]["top_paths"] == "/v1/chat/completions (1)"
    assert rule._paths.untracked == 0


async def test_the_sample_cap_bounds_a_window_and_the_card_says_so(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_rules, "alert_on_transition", _recorder(calls))
    monkeypatch.setattr(alert_rules, "_MAX_WINDOW_SAMPLES", 50)
    clock = [1_000_000.0]
    monkeypatch.setattr(alert_rules.time, "time", lambda: clock[0])
    rule = FailedRequestRateRule(RateRule(window_sec=3600, min_samples=10, threshold_pct=10.0))

    for i in range(200):
        clock[0] += 1
        await rule.on_record(_request(500 if i % 4 == 0 else 200))

    # The latest 50 are records 150-199, and 12 of those failed.
    assert len(rule._window.window._items) == 50
    assert rule._window.marked == 12
    card = calls[-1]["context"]
    assert card["rate"] == (
        "24.0% (12 of 50 requests, last 3600s; only the latest 50 samples are kept)"
    )
    # Once the clock, not the cap, bounds the window again, the note goes.
    clock[0] += 3601
    for _ in range(10):
        await rule.on_record(_request(200))
    assert calls[-1]["context"]["rate"] == "0.0% (0 of 10 requests, last 3600s)"


async def test_the_sample_cap_leaves_a_count_rules_verdict_alone(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_rules, "alert_on_transition", _recorder(calls))
    monkeypatch.setattr(alert_rules, "_MAX_WINDOW_SAMPLES", 20)
    rule = ClientErrorBurstRule(ClientErrorBurstConfig(window_sec=600, threshold_count=12))
    for _ in range(100):
        await rule.on_record(
            _record(name="x", event="client_error_skip_breaker", endpoint_id="m:a", status=400)
        )
    assert all(call["breached"] for call in calls[12:])
    assert calls[-1]["context"]["count"] == 20


# --------------------------------------------------------------------------- #
# The drain lets the loop run while a backlog is queued.
# --------------------------------------------------------------------------- #


async def test_the_drain_yields_while_a_backlog_is_queued():
    handler = AlertingLogHandler(maxsize=10_000)
    engine = AlertEngine(
        handler=handler, config=AlertConfig(), scheduler=None, op_store=None, log_store=None
    )
    batch = alert_rules._DRAIN_YIELD_EVERY
    backlog = 5 * batch
    for _ in range(backlog):
        handler.queue.put_nowait(_request(200))

    seen: list[int] = []
    await engine.start()
    try:
        for _ in range(10 * backlog):
            if not handler.queue.qsize():
                break
            await asyncio.sleep(0)
            seen.append(handler.queue.qsize())
    finally:
        await engine.stop()

    assert seen[-1] == 0
    # Other tasks ran while records were still queued, never more than one
    # batch apart.
    assert any(0 < size < backlog for size in seen)
    drained_between_turns = [
        before - after for before, after in zip([backlog, *seen], seen, strict=False)
    ]
    assert max(drained_between_turns) <= batch
