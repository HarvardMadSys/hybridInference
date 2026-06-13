"""Tests for the RouteWiseRouter._pending_decisions TTL sweep.

PR 2 of issue #4: covers the periodic-cleanup task, the eviction event
emission, and the start/stop lifecycle hooks added to ``RouteWiseRouter``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import MagicMock

import pytest

from routing.routewise import router as router_module
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import (
    PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS,
    PENDING_DECISIONS_TTL_SECONDS,
    RouteWiseRouter,
)
from serving.utils.logging import JsonFormatter


class _FakeRouteConfig:
    def __init__(self, adapters: list) -> None:
        self.adapters = adapters


class _FakeFixedRouter:
    def __init__(self) -> None:
        self.routes: dict[str, _FakeRouteConfig] = {}

    def add(self, model_id: str, adapters_with_weights: list) -> None:
        self.routes[model_id] = _FakeRouteConfig(adapters=adapters_with_weights)


def _make_adapter(
    provider_type: str = "on_demand",
    prompt_price: str = "1.0",
    completion_price: str = "2.0",
    endpoint_id: str = "m:p",
) -> MagicMock:
    cfg = MagicMock()
    cfg.id = "m"
    cfg.provider_type = provider_type
    cfg.endpoint_id = endpoint_id
    cfg.pricing = {"prompt": prompt_price, "completion": completion_price}
    adapter = MagicMock()
    adapter.config = cfg
    return adapter


def _make_router() -> RouteWiseRouter:
    """Build a minimal RouteWiseRouter with one S_A adapter."""
    fr = _FakeFixedRouter()
    fr.add("m", [(_make_adapter(), 1.0)])
    return RouteWiseRouter(fixed_router=fr, config=RouteWiseConfig())


@pytest.mark.unit
async def test_pending_decisions_evicted_after_ttl(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entries older than TTL_SECONDS get evicted; fresh entries stay."""
    router = _make_router()
    now = time.time()
    router._pending_decisions["req-old"] = {
        "timestamp": now - PENDING_DECISIONS_TTL_SECONDS - 100.0
    }
    router._pending_decisions["req-fresh"] = {"timestamp": now}

    with caplog.at_level(logging.INFO, logger="routing.routewise.router"):
        evicted = await router._sweep_pending_decisions_once()

    assert evicted == 1
    assert "req-old" not in router._pending_decisions
    assert "req-fresh" in router._pending_decisions
    matching = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "routewise_decision_evicted"
        and getattr(r, "request_id", None) == "req-old"
    ]
    assert len(matching) == 1
    assert matching[0].age_sec >= int(PENDING_DECISIONS_TTL_SECONDS)


@pytest.mark.unit
async def test_pending_decisions_no_eviction_within_ttl(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entries younger than TTL_SECONDS are not evicted and emit no event."""
    router = _make_router()
    now = time.time()
    router._pending_decisions["req-fresh"] = {"timestamp": now - 30.0}

    with caplog.at_level(logging.INFO, logger="routing.routewise.router"):
        evicted = await router._sweep_pending_decisions_once()

    assert evicted == 0
    assert "req-fresh" in router._pending_decisions
    matching = [
        r for r in caplog.records if getattr(r, "event", None) == "routewise_decision_evicted"
    ]
    assert matching == []


@pytest.mark.unit
async def test_pending_decisions_race_pop_returns_none_no_eviction_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Race: request completion pops a stale entry between iteration and pop.

    The sweep collects ``stale`` while holding the lock, but the
    request-completion paths in ``chat_completion`` /
    ``stream_chat_completion`` call ``pop`` *without* the lock. So an entry
    we picked as stale may be gone by the time the sweep tries to pop it.
    In that case the sweep must NOT count the entry as evicted and must NOT
    emit ``routewise_decision_evicted`` (the request consumed it normally).
    """
    router = _make_router()
    now = time.time()
    stale_ts = now - PENDING_DECISIONS_TTL_SECONDS - 100.0

    # Simulate the race: use a dict subclass whose ``pop`` always returns
    # ``None`` for our key, mimicking a concurrent ``record_observation``
    # that already consumed the entry between the iterate-step and the
    # pop-step inside the sweep.
    pop_calls: list[str] = []

    class RacyDict(dict):
        def pop(self, key, default=None):  # type: ignore[override]
            pop_calls.append(key)
            return default  # entry already gone — concurrent consumer won

    racy = RacyDict()
    racy["req-old"] = {"timestamp": stale_ts}
    router._pending_decisions = racy

    with caplog.at_level(logging.INFO, logger="routing.routewise.router"):
        evicted = await router._sweep_pending_decisions_once()

    # The sweep tried to pop ``req-old``, but it was already gone — race lost.
    assert pop_calls == ["req-old"]
    assert evicted == 0
    matching = [
        r for r in caplog.records if getattr(r, "event", None) == "routewise_decision_evicted"
    ]
    assert matching == []


@pytest.mark.unit
async def test_pending_decisions_skips_entries_without_timestamp(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive: entries with a missing/non-numeric timestamp are left alone."""
    router = _make_router()
    router._pending_decisions["req-bad"] = {}  # no timestamp
    router._pending_decisions["req-bad-type"] = {"timestamp": "not-a-number"}

    with caplog.at_level(logging.INFO, logger="routing.routewise.router"):
        evicted = await router._sweep_pending_decisions_once()

    assert evicted == 0
    assert "req-bad" in router._pending_decisions
    assert "req-bad-type" in router._pending_decisions


@pytest.mark.unit
async def test_select_adapter_populates_timestamp() -> None:
    """All four assignment branches in _select_adapter populate ``timestamp``."""
    router = _make_router()
    request_id = "req-1"
    router._select_adapter("m", {"request_id": request_id})
    assert request_id in router._pending_decisions
    decision = router._pending_decisions[request_id]
    assert "timestamp" in decision
    assert isinstance(decision["timestamp"], (int, float))


@pytest.mark.unit
async def test_start_stop_lifecycle() -> None:
    """``start()`` schedules the sweep task; ``stop()`` cancels it."""
    router = _make_router()
    assert router._sweep_task is None

    await router.start()
    task = router._sweep_task
    assert task is not None
    assert not task.done()

    # Idempotent: calling start again does not replace the running task.
    await router.start()
    assert router._sweep_task is task

    await router.stop()
    assert router._sweep_task is None
    assert task.cancelled() or task.done()


@pytest.mark.unit
async def test_stop_without_start_is_noop() -> None:
    """Calling ``stop()`` before ``start()`` must not raise."""
    router = _make_router()
    await router.stop()  # should not raise
    assert router._sweep_task is None


@pytest.mark.unit
async def test_sweep_loop_invokes_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The internal loop calls ``_sweep_pending_decisions_once`` after the interval."""
    router = _make_router()

    call_count = 0

    async def fake_sweep() -> int:
        nonlocal call_count
        call_count += 1
        return 0

    # Speed up the loop and replace the actual sweep so the test runs fast.
    monkeypatch.setattr(
        "routing.routewise.router.PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(router, "_sweep_pending_decisions_once", fake_sweep)

    await router.start()
    # Give the loop time to fire at least once.
    await asyncio.sleep(0.05)
    await router.stop()

    assert call_count >= 1


@pytest.mark.unit
def test_module_constants() -> None:
    """Defaults align with the spec: 300s TTL, 60s sweep interval."""
    assert PENDING_DECISIONS_TTL_SECONDS == 300.0
    assert PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS == 60.0


@pytest.mark.unit
async def test_streaming_decisions_are_not_ttl_evicted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Long-lived streaming requests should not be treated as leaked decisions."""
    router = _make_router()
    now = time.time()
    router._pending_decisions["req-stream"] = {
        "timestamp": now - PENDING_DECISIONS_TTL_SECONDS - 100.0,
        "is_streaming": True,
    }

    with caplog.at_level(logging.INFO, logger="routing.routewise.router"):
        evicted = await router._sweep_pending_decisions_once()

    assert evicted == 0
    assert "req-stream" in router._pending_decisions
    matching = [
        r for r in caplog.records if getattr(r, "event", None) == "routewise_decision_evicted"
    ]
    assert matching == []


@pytest.mark.unit
async def test_sweep_loop_survives_sweep_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing sweep must be logged and retried, not kill the loop forever."""
    router = _make_router()

    calls = 0

    async def flaky_sweep() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(
        "routing.routewise.router.PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(router, "_sweep_pending_decisions_once", flaky_sweep)

    await router.start()
    await asyncio.sleep(0.05)
    sweep_task = router._sweep_task
    await router.stop()

    # The loop kept running past the first raising call.
    assert calls >= 2
    # The task ended via cancellation (stop), not because the exception escaped.
    assert sweep_task is not None
    assert not (sweep_task.done() and sweep_task.exception() is not None)


@pytest.mark.unit
def test_pending_decisions_size_cap_evicts_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hard cap bounds the map by dropping the oldest entries, keeping the newest."""
    router = _make_router()
    monkeypatch.setattr(router_module, "_PENDING_DECISIONS_MAX", 3)

    for i in range(5):
        router._store_pending_decision(f"req-{i}", {"timestamp": float(i)})

    assert len(router._pending_decisions) == 3
    # Oldest three were evicted; newest three remain.
    assert set(router._pending_decisions) == {"req-2", "req-3", "req-4"}


@pytest.mark.unit
def test_pending_decisions_size_cap_never_drops_just_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even at cap, the entry just stored is retained (the just-inserted req wins)."""
    router = _make_router()
    monkeypatch.setattr(router_module, "_PENDING_DECISIONS_MAX", 1)

    router._store_pending_decision("first", {"timestamp": 1.0})
    router._store_pending_decision("second", {"timestamp": 2.0})

    assert "second" in router._pending_decisions
    assert len(router._pending_decisions) == 1


@pytest.mark.unit
async def test_eviction_age_sec_survives_json_formatting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Eviction records keep ``age_sec`` when LOG_FORMAT=json uses JsonFormatter."""
    router = _make_router()
    now = time.time()
    router._pending_decisions["req-old"] = {
        "timestamp": now - PENDING_DECISIONS_TTL_SECONDS - 100.0,
    }

    with caplog.at_level(logging.INFO, logger="routing.routewise.router"):
        await router._sweep_pending_decisions_once()

    record = next(
        r for r in caplog.records if getattr(r, "event", None) == "routewise_decision_evicted"
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["age_sec"] >= int(PENDING_DECISIONS_TTL_SECONDS)
