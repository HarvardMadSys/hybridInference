"""The refreshing cache behind the Providers tab's slow admin views.

``_refreshing_cache.RefreshingResult`` keeps one expensive result per process:
the first request waits, a fresh result is reused, a stale one is served while
one background refresh runs, an explicit refresh waits, and a failed refresh
keeps the previous result. Each case is pinned here with a loader the test
controls; the endpoints' use of it is in ``test_admin_performance_metrics_cache``
and ``test_admin_ttft_scatter_cache``.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from serving.servers.routers.admin._refreshing_cache import RefreshingResult

pytestmark = pytest.mark.asyncio


class _Loader:
    """Returns ``result-1``, ``result-2``, ...; fails or blocks when told to."""

    def __init__(self) -> None:
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
        return f"result-{call}"


def _stale(cache: RefreshingResult) -> None:
    cache._computed_at -= cache.fresh_seconds + 1


async def _settle() -> None:
    """Let a background refresh task run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_first_request_waits_then_a_fresh_result_is_reused():
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    assert await cache.get("db", load) == "result-1"
    assert await cache.get("db", load) == "result-1"
    assert load.calls == 1


async def test_a_stale_result_is_served_at_once_and_refreshed_once():
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    await cache.get("db", load)
    _stale(cache)
    load.gate = asyncio.Event()

    # Neither stale read waits; together they start one refresh.
    assert await cache.get("db", load) == "result-1"
    assert await cache.get("db", load) == "result-1"
    await _settle()
    assert load.calls == 2

    load.gate.set()
    await _settle()
    assert await cache.get("db", load) == "result-2"
    assert load.calls == 2


async def test_an_explicit_refresh_waits_for_a_new_result():
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    await cache.get("db", load)
    assert await cache.get("db", load, refresh=True) == "result-2"
    assert await cache.get("db", load) == "result-2"


async def test_a_failed_background_refresh_keeps_the_previous_result(caplog):
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    await cache.get("db", load)
    _stale(cache)
    load.fail = True

    with caplog.at_level(logging.ERROR):
        assert await cache.get("db", load) == "result-1"
        await _settle()
    assert "keeping the previous result" in caplog.text
    # Still stale, so the next read tries again -- and serves the old result meanwhile.
    assert await cache.get("db", load) == "result-1"
    await _settle()
    assert load.calls == 3

    load.fail = False
    await cache.get("db", load)
    await _settle()
    assert await cache.get("db", load) == "result-4"


async def test_a_failed_explicit_refresh_returns_the_previous_result():
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    await cache.get("db", load)
    load.fail = True
    assert await cache.get("db", load, refresh=True) == "result-1"


async def test_a_failed_first_computation_reaches_the_caller_and_is_retried():
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    load.fail = True
    with pytest.raises(RuntimeError, match="load 1 failed"):
        await cache.get("db", load)
    load.fail = False
    assert await cache.get("db", load) == "result-2"


async def test_concurrent_first_requests_share_one_computation():
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    load.gate = asyncio.Event()
    waiters = [asyncio.create_task(cache.get("db", load)) for _ in range(5)]
    await _settle()
    assert load.calls == 1
    load.gate.set()
    assert await asyncio.gather(*waiters) == ["result-1"] * 5


async def test_a_waiter_that_goes_away_does_not_cancel_the_computation():
    """A client disconnecting must not cost the other waiters their result."""
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
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
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    assert await cache.get("db-a", load) == "result-1"
    assert await cache.get("db-b", load) == "result-2"


async def test_a_result_computed_for_a_replaced_key_is_dropped():
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    load.gate = asyncio.Event()
    old = asyncio.create_task(cache.get("db-a", load))
    await _settle()
    other = _Loader()
    assert await cache.get("db-b", other) == "result-1"
    load.gate.set()
    assert await old == "result-1"
    # db-a's late result did not overwrite db-b's.
    assert cache._value == "result-1" and cache._key == "db-b"
    assert other.calls == 1


async def test_warm_up_fills_the_cache_and_only_logs_a_failure(caplog):
    cache, load = RefreshingResult("t", fresh_seconds=60), _Loader()
    load.fail = True
    with caplog.at_level(logging.ERROR):
        await cache.warm("db", load)
    assert "warm-up failed" in caplog.text

    load.fail = False
    await cache.warm("db", load)
    assert await cache.get("db", load) == "result-2"
    assert load.calls == 2
