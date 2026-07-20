"""Lifecycle and observability contracts for pending prefix-cache state."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from routing.routewise import router as router_module
from routing.routewise.config import RouteWiseConfig
from routing.routewise.prefix_cache_pending import PendingPrefixCacheStore
from routing.routewise.router import RouteWiseRouter


@pytest.mark.unit
def test_store_put_pop_and_terminal_discard_are_normal_cleanup(caplog):
    now = 100.0
    store = PendingPrefixCacheStore(clock=lambda: now)
    store.put("success", ("block",), {"endpoint": "scope"})
    store.put("failure", ("other",), {"endpoint": "scope"})

    entry = store.pop("success")
    assert entry is not None
    assert entry.blocks == ("block",)
    assert entry.scopes == {"endpoint": "scope"}
    assert store.discard("failure") is True
    assert len(store) == 0
    assert not [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "routewise_prefix_cache_entry_evicted"
    ]


@pytest.mark.unit
def test_expired_entry_is_rejected_on_pop_and_emits_eviction(caplog):
    now = 100.0
    store = PendingPrefixCacheStore(ttl_seconds=10.0, clock=lambda: now)
    store.put("expired", ("block",), {"endpoint": "scope"})
    now = 111.0

    with caplog.at_level(logging.INFO):
        assert store.pop("expired") is None

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "routewise_prefix_cache_entry_evicted"
    )
    assert record.request_id == "expired"
    assert record.reason == "ttl"
    assert record.age_sec == 11


@pytest.mark.unit
def test_sweep_reclaims_only_expired_entries(caplog):
    now = 100.0
    store = PendingPrefixCacheStore(ttl_seconds=10.0, clock=lambda: now)
    store.put("old", ("block",), {"endpoint": "scope"})
    now = 105.0
    store.put("fresh", ("block",), {"endpoint": "scope"})
    now = 111.0

    with caplog.at_level(logging.INFO):
        assert store.sweep_expired() == 1

    assert "old" not in store
    assert "fresh" in store
    assert store.sweep_expired() == 0


@pytest.mark.unit
def test_size_cap_evicts_oldest_and_reports_reason(caplog):
    now = 100.0
    store = PendingPrefixCacheStore(max_entries=2, clock=lambda: now)
    store.put("first", ("a",), {"endpoint": "scope"})
    now = 101.0
    store.put("second", ("b",), {"endpoint": "scope"})
    now = 102.0

    with caplog.at_level(logging.INFO):
        store.put("third", ("c",), {"endpoint": "scope"})

    assert "first" not in store
    assert "second" in store
    assert "third" in store
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "routewise_prefix_cache_entry_evicted"
    )
    assert record.request_id == "first"
    assert record.reason == "size_cap"
    assert record.age_sec == 2
    assert record.pending_count == 2
    assert record.capacity == 2


@pytest.mark.unit
def test_replacing_request_preserves_original_ttl_and_fifo_position(caplog):
    now = 100.0
    store = PendingPrefixCacheStore(ttl_seconds=10.0, max_entries=2, clock=lambda: now)
    store.put("retry", ("old",), {"endpoint": "old-scope"})
    now = 105.0
    store.put("other", ("other",), {"endpoint": "scope"})
    now = 109.0
    store.put("retry", ("new",), {"endpoint": "new-scope"})
    store.put("third", ("third",), {"endpoint": "scope"})

    assert "retry" not in store
    assert "other" in store
    assert "third" in store

    caplog.clear()
    now = 100.0
    store = PendingPrefixCacheStore(ttl_seconds=10.0, clock=lambda: now)
    store.put("retry", ("old",), {"endpoint": "old-scope"})
    now = 115.0
    store.put("retry", ("new",), {"endpoint": "new-scope"})

    with caplog.at_level(logging.INFO):
        assert store.pop("retry") is None

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "routewise_prefix_cache_entry_evicted"
    )
    assert record.age_sec == 15


@pytest.mark.unit
def test_stream_activity_renews_inactivity_ttl_without_changing_age(caplog):
    now = 100.0
    store = PendingPrefixCacheStore(ttl_seconds=10.0, clock=lambda: now)
    store.put("stream", ("block",), {"endpoint": "scope"})
    now = 109.0
    assert store.touch("stream") is True
    now = 115.0

    assert store.sweep_expired() == 0
    assert "stream" in store

    now = 120.0
    with caplog.at_level(logging.INFO):
        assert store.sweep_expired() == 1
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "routewise_prefix_cache_entry_evicted"
    )
    assert record.age_sec == 20
    assert record.idle_sec == 11


@pytest.mark.unit
def test_touch_moves_active_entry_behind_older_inactive_entry():
    now = 100.0
    store = PendingPrefixCacheStore(max_entries=2, clock=lambda: now)
    store.put("first", ("first",), {"endpoint": "scope"})
    now = 101.0
    store.put("second", ("second",), {"endpoint": "scope"})
    now = 102.0
    assert store.touch("first") is True
    store.put("third", ("third",), {"endpoint": "scope"})

    assert "first" in store
    assert "second" not in store
    assert "third" in store


@pytest.mark.unit
def test_capacity_reclaims_expired_entries_as_ttl(caplog):
    now = 100.0
    store = PendingPrefixCacheStore(ttl_seconds=10.0, max_entries=1, clock=lambda: now)
    store.put("expired", ("old",), {"endpoint": "scope"})
    now = 111.0

    with caplog.at_level(logging.INFO):
        store.put("new", ("new",), {"endpoint": "scope"})

    assert "expired" not in store
    assert "new" in store
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "routewise_prefix_cache_entry_evicted"
    )
    assert record.reason == "ttl"


@pytest.mark.unit
def test_pop_and_expiry_sweep_race_emits_one_eviction(caplog):
    now = 100.0
    store = PendingPrefixCacheStore(ttl_seconds=10.0, clock=lambda: now)
    store.put("request", ("block",), {"endpoint": "scope"})
    now = 111.0
    barrier = Barrier(2)

    def _pop():
        barrier.wait()
        return store.pop("request")

    def _sweep():
        barrier.wait()
        return store.sweep_expired()

    with caplog.at_level(logging.INFO), ThreadPoolExecutor(max_workers=2) as executor:
        popped = executor.submit(_pop)
        swept = executor.submit(_sweep)
        assert popped.result() is None
        assert swept.result() in (0, 1)

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "routewise_prefix_cache_entry_evicted"
    ]
    assert len(records) == 1
    assert len(store) == 0


@pytest.mark.unit
def test_clear_is_normal_cleanup_without_eviction(caplog):
    store = PendingPrefixCacheStore()
    store.put("request", ("block",), {"endpoint": "scope"})

    with caplog.at_level(logging.INFO):
        store.clear()

    assert len(store) == 0
    assert not [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "routewise_prefix_cache_entry_evicted"
    ]


@pytest.mark.asyncio
async def test_router_runs_prefix_sweep_only_when_feature_enabled(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "PREFIX_CACHE_PENDING_SWEEP_INTERVAL_SECONDS",
        0.001,
    )
    enabled = RouteWiseRouter(config=RouteWiseConfig(prefix_cache_cost_adjustment_enabled=True))
    swept = asyncio.Event()
    monkeypatch.setattr(
        enabled.pending_prefix_cache,
        "sweep_expired",
        lambda: swept.set() or 0,
    )

    try:
        await enabled.start()
        await asyncio.wait_for(swept.wait(), timeout=1.0)
        assert enabled._prefix_cache_sweep_task is not None
    finally:
        await enabled.stop()
    assert enabled._prefix_cache_sweep_task is None

    disabled = RouteWiseRouter(config=RouteWiseConfig())
    await disabled.start()
    try:
        assert disabled._prefix_cache_sweep_task is None
    finally:
        await disabled.stop()
