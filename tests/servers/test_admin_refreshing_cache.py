"""The refreshing cache behind the Providers tab's slow admin views.

``_refreshing_cache.RefreshingResult`` keeps one expensive result per process,
and through a ``ResultStore`` across restarts. No request waits for a refresh:
a stale result and an explicit refresh both return the current result at once
and start one computation, flagged by ``refreshing``. Only a request finding no
result at all waits, and for at most ``first_wait_seconds``. A failed refresh
keeps the previous result. Each case is pinned here with a loader and a store
the test controls; the endpoints' use of it is in
``test_admin_performance_metrics_cache`` and ``test_admin_ttft_scatter_cache``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from serving.servers.routers.admin._refreshing_cache import RefreshingResult

pytestmark = pytest.mark.asyncio


class _Loader:
    """Returns ``result-1``, ``result-2``, ...; fails or blocks when told to."""

    def __init__(self, prefix: str = "result") -> None:
        self.prefix = prefix
        self.calls = 0
        self.fail = False
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> str:
        self.calls += 1
        call = self.calls
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError(f"load {call} failed")
        return f"{self.prefix}-{call}"


class _Store:
    """An in-memory ResultStore; reads or writes fail when told to."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, datetime]] = {}
        self.reads = 0
        self.fail_read = False
        self.fail_write = False

    async def read(self, key):
        self.reads += 1
        if self.fail_read:
            raise ConnectionError("database unavailable")
        return self.rows.get(key)

    async def write(self, key, value, computed_at):
        if self.fail_write:
            raise ConnectionError("database unavailable")
        self.rows[key] = (value, computed_at)


def _cache(store: _Store | None = None, *, first_wait: float = 5.0) -> RefreshingResult:
    return RefreshingResult("t", fresh_seconds=60, first_wait_seconds=first_wait, store=store)


def _stale(cache: RefreshingResult) -> None:
    cache._computed_at -= cache.fresh_seconds + 1


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


async def _settle() -> None:
    """Let a background refresh task run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_first_request_waits_then_a_fresh_result_is_reused():
    cache, load = _cache(), _Loader()
    assert await cache.get("db", load) == "result-1"
    assert await cache.get("db", load) == "result-1"
    assert load.calls == 1
    assert not cache.refreshing


async def test_a_stale_result_is_served_at_once_and_refreshed_once():
    cache, load = _cache(), _Loader()
    await cache.get("db", load)
    _stale(cache)
    load.gate = asyncio.Event()

    # Neither stale read waits; together they start one refresh.
    assert await cache.get("db", load) == "result-1"
    assert await cache.get("db", load) == "result-1"
    await _settle()
    assert load.calls == 2
    assert cache.refreshing

    load.gate.set()
    await _settle()
    assert not cache.refreshing
    assert await cache.get("db", load) == "result-2"
    assert load.calls == 2


async def test_an_explicit_refresh_returns_at_once_and_runs_in_the_background():
    cache, load = _cache(), _Loader()
    await cache.get("db", load)
    load.gate = asyncio.Event()

    assert await cache.get("db", load, refresh=True) == "result-1"
    assert cache.refreshing
    # A second refresh while one runs joins it.
    assert await cache.get("db", load, refresh=True) == "result-1"
    await _settle()
    assert load.calls == 2

    load.gate.set()
    await _settle()
    assert await cache.get("db", load) == "result-2"


async def test_a_failed_background_refresh_keeps_the_previous_result(caplog):
    cache, load = _cache(), _Loader()
    await cache.get("db", load)
    _stale(cache)
    load.fail = True

    with caplog.at_level(logging.ERROR):
        assert await cache.get("db", load) == "result-1"
        await _settle()
    assert "keeping the previous result" in caplog.text
    assert not cache.refreshing
    # Still stale, so the next read tries again -- and serves the old result meanwhile.
    assert await cache.get("db", load) == "result-1"
    await _settle()
    assert load.calls == 3

    load.fail = False
    await cache.get("db", load)
    await _settle()
    assert await cache.get("db", load) == "result-4"


async def test_a_failed_explicit_refresh_keeps_the_previous_result():
    cache, load = _cache(), _Loader()
    await cache.get("db", load)
    load.fail = True
    assert await cache.get("db", load, refresh=True) == "result-1"
    await _settle()
    assert await cache.get("db", load) == "result-1"


async def test_a_failed_first_computation_reaches_the_caller_and_is_retried():
    cache, load = _cache(), _Loader()
    load.fail = True
    with pytest.raises(RuntimeError, match="load 1 failed"):
        await cache.get("db", load)
    load.fail = False
    assert await cache.get("db", load) == "result-2"


async def test_a_first_computation_that_outlasts_the_wait_returns_none_and_goes_on():
    cache, load = _cache(first_wait=0.01), _Loader()
    load.gate = asyncio.Event()

    assert await cache.get("db", load) is None
    assert cache.refreshing
    # Asking again neither waits longer than the limit nor starts another.
    assert await cache.get("db", load) is None
    assert load.calls == 1

    load.gate.set()
    await _settle()
    assert await cache.get("db", load) == "result-1"


async def test_concurrent_first_requests_share_one_computation():
    cache, load = _cache(), _Loader()
    load.gate = asyncio.Event()
    waiters = [asyncio.create_task(cache.get("db", load)) for _ in range(5)]
    await _settle()
    assert load.calls == 1
    load.gate.set()
    assert await asyncio.gather(*waiters) == ["result-1"] * 5


async def test_a_waiter_that_goes_away_does_not_cancel_the_computation():
    """A client disconnecting must not cost the other waiters their result."""
    cache, load = _cache(), _Loader()
    load.gate = asyncio.Event()
    leaving = asyncio.create_task(cache.get("db", load))
    staying = asyncio.create_task(cache.get("db", load))
    await _settle()
    leaving.cancel()
    await _settle()
    load.gate.set()
    assert await staying == "result-1"
    assert load.calls == 1


async def test_a_different_key_never_sees_another_keys_result():
    cache, load = _cache(), _Loader()
    assert await cache.get("db-a", load) == "result-1"
    assert await cache.get("db-b", load) == "result-2"


async def test_a_result_computed_for_a_replaced_key_is_dropped():
    store = _Store()
    cache, load = _cache(store), _Loader()
    load.gate = asyncio.Event()
    old = asyncio.create_task(cache.get("db-a", load))
    await _settle()
    other = _Loader()
    assert await cache.get("db-b", other) == "result-1"
    load.gate.set()
    assert await old == "result-1"
    # db-a's late result overwrote neither db-b's in memory nor anything stored.
    assert cache._value == "result-1" and cache._key == "db-b"
    assert set(store.rows) == {"db-b"}
    assert other.calls == 1


async def test_each_result_is_stored_with_when_it_was_computed():
    store = _Store()
    cache, load = _cache(store), _Loader()
    before = datetime.now(timezone.utc)
    await cache.get("db", load)
    value, computed_at = store.rows["db"]
    assert value == "result-1"
    assert before <= computed_at <= datetime.now(timezone.utc)


async def test_a_new_process_serves_the_stored_result_without_computing():
    store = _Store()
    store.rows["db"] = ("stored", _ago(10))
    cache, load = _cache(store), _Loader()

    assert await cache.get("db", load) == "stored"
    assert await cache.get("db", load) == "stored"
    assert load.calls == 0
    # Read once, not on every request.
    assert store.reads == 1


async def test_a_stale_stored_result_is_served_while_it_is_refreshed():
    store = _Store()
    store.rows["db"] = ("stored", _ago(600))
    cache, load = _cache(store), _Loader()
    load.gate = asyncio.Event()

    assert await cache.get("db", load) == "stored"
    assert cache.refreshing
    load.gate.set()
    await _settle()
    assert await cache.get("db", load) == "result-1"
    assert store.rows["db"][0] == "result-1"


async def test_an_unreadable_store_falls_back_to_computing(caplog):
    store = _Store()
    store.fail_read = True
    cache, load = _cache(store), _Loader()
    with caplog.at_level(logging.WARNING):
        assert await cache.get("db", load) == "result-1"
    assert "could not read the stored result" in caplog.text


async def test_an_unwritable_store_still_keeps_the_result_in_memory(caplog):
    store = _Store()
    store.fail_write = True
    cache, load = _cache(store), _Loader()
    with caplog.at_level(logging.WARNING):
        assert await cache.get("db", load) == "result-1"
    assert "could not store the result" in caplog.text
    assert await cache.get("db", load) == "result-1"
    assert load.calls == 1


async def test_warm_up_fills_the_cache_and_only_logs_a_failure(caplog):
    cache, load = _cache(), _Loader()
    load.fail = True
    with caplog.at_level(logging.ERROR):
        await cache.warm("db", load)
    assert "warm-up failed" in caplog.text

    load.fail = False
    await cache.warm("db", load)
    assert await cache.get("db", load) == "result-2"
    assert load.calls == 2


async def test_warm_up_keeps_a_fresh_stored_result_and_refreshes_a_stale_one():
    store = _Store()
    store.rows["db"] = ("stored", _ago(10))
    cache, load = _cache(store), _Loader()
    await cache.warm("db", load)
    assert load.calls == 0
    assert await cache.get("db", load) == "stored"

    stale_store = _Store()
    stale_store.rows["db"] = ("stored", _ago(600))
    cache, load = _cache(stale_store), _Loader()
    # Waits for the refresh, so warm-ups run one after another.
    await cache.warm("db", load)
    assert load.calls == 1
    assert not cache.refreshing
    assert await cache.get("db", load) == "result-1"
