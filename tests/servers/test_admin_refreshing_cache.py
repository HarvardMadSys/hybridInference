"""The refreshing cache behind the Providers tab's slow admin views.

``_refreshing_cache.RefreshingResult`` keeps one expensive result per key, and
through a ``ResultStore`` across restarts. No request waits for a refresh: a
stale result and an explicit refresh both return the current result at once and
start one computation, flagged by ``refreshing``. Only a request finding no
result at all waits, and for at most ``first_wait_seconds``. A failed
computation keeps the previous result, is reported, and is not retried on a
stale read for ``retry_seconds``. A delete advancing the store's generation
drops results computed before it, and stops a running computation publishing.
Each case is pinned here with a loader and a store the test controls; the
endpoints' use of it is in ``test_admin_performance_metrics_cache`` and
``test_admin_ttft_scatter_cache``, the real store in
``tests/integration/test_admin_performance_views_postgres.py``.
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
        self.fail: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> str:
        self.calls += 1
        call = self.calls
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return f"{self.prefix}-{call}"


class _Store:
    """An in-memory ResultStore with a generation; fails or blocks when told to."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, datetime, int]] = {}
        self.current = 0
        self.reads = 0
        self.fail_read = False
        self.fail_write = False
        self.read_gate: dict[str, asyncio.Event] = {}
        # Holds write() after it stored, as returning the connection to the pool can.
        self.after_write: asyncio.Event | None = None

    async def generation(self, key):
        return self.current

    async def read(self, key):
        self.reads += 1
        if key in self.read_gate:
            await self.read_gate[key].wait()
        if self.fail_read:
            raise ConnectionError("database unavailable")
        return self.rows.get(key)

    async def write(self, key, value, computed_at, generation):
        if self.fail_write:
            raise ConnectionError("database unavailable")
        if generation != self.current:
            return False
        self.rows[key] = (value, computed_at, generation)
        if self.after_write is not None:
            await self.after_write.wait()
        return True

    def delete(self) -> None:
        """What a hard delete does: advance the generation, drop the results."""
        self.current += 1
        self.rows.clear()


def _cache(store: _Store | None = None, *, first_wait: float = 5.0) -> RefreshingResult:
    return RefreshingResult(
        "t", fresh_seconds=60, first_wait_seconds=first_wait, retry_seconds=30, store=store
    )


async def _value(cache: RefreshingResult, key, load, **kwargs):
    return (await cache.get(key, load, **kwargs)).value


def _stale(cache: RefreshingResult, key="db") -> None:
    cache._states[key].computed_at -= cache.fresh_seconds + 1


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


async def _settle() -> None:
    """Let a background refresh task run to completion."""
    for _ in range(10):
        await asyncio.sleep(0)


async def test_first_request_waits_then_a_fresh_result_is_reused():
    cache, load = _cache(), _Loader()
    served = await cache.get("db", load)
    assert (served.value, served.refreshing, served.refresh_error) == ("result-1", False, None)
    assert await _value(cache, "db", load) == "result-1"
    assert load.calls == 1


async def test_a_stale_result_is_served_at_once_and_refreshed_once():
    cache, load = _cache(), _Loader()
    await cache.get("db", load)
    _stale(cache)
    load.gate = asyncio.Event()

    # Neither stale read waits; together they start one refresh.
    first = await cache.get("db", load)
    assert (first.value, first.refreshing) == ("result-1", True)
    assert await _value(cache, "db", load) == "result-1"
    await _settle()
    assert load.calls == 2

    load.gate.set()
    await _settle()
    after = await cache.get("db", load)
    assert (after.value, after.refreshing) == ("result-2", False)
    assert load.calls == 2


async def test_an_explicit_refresh_returns_at_once_and_runs_in_the_background():
    cache, load = _cache(), _Loader()
    await cache.get("db", load)
    load.gate = asyncio.Event()

    assert await _value(cache, "db", load, refresh=True) == "result-1"
    # A second refresh while one runs joins it.
    assert (await cache.get("db", load, refresh=True)).refreshing
    await _settle()
    assert load.calls == 2

    load.gate.set()
    await _settle()
    assert await _value(cache, "db", load) == "result-2"


async def test_a_failed_refresh_keeps_the_result_says_why_and_is_not_rerun_on_each_poll():
    """Polling a stale result whose refresh fails must end, not rerun the query."""
    cache, load = _cache(), _Loader()
    await cache.get("db", load)
    _stale(cache)
    load.fail = RuntimeError("statement timeout")

    await cache.get("db", load)  # starts the refresh, which fails
    await _settle()
    for _ in range(3):  # the console's polls
        served = await cache.get("db", load)
        await _settle()
        assert (served.value, served.refreshing) == ("result-1", False)
        assert served.refresh_error == "statement timeout"
    assert load.calls == 2

    # After the retry interval a stale read tries again; success clears the error.
    load.fail = None
    cache._states["db"].failed_at -= cache.retry_seconds
    await cache.get("db", load)
    await _settle()
    served = await cache.get("db", load)
    assert (served.value, served.refresh_error) == ("result-3", None)


async def test_an_explicit_refresh_retries_at_once_even_after_a_failure():
    cache, load = _cache(), _Loader()
    await cache.get("db", load)
    load.fail = RuntimeError("statement timeout")
    await cache.get("db", load, refresh=True)
    await _settle()
    load.fail = None
    assert (await cache.get("db", load, refresh=True)).refreshing
    await _settle()
    assert await _value(cache, "db", load) == "result-3"


async def test_a_failed_first_computation_reaches_the_caller_and_is_retried_later():
    cache, load = _cache(), _Loader()
    load.fail = RuntimeError("load failed")
    with pytest.raises(RuntimeError, match="load failed"):
        await cache.get("db", load)
    # A poll within the retry interval gets the failure again, without a rerun.
    with pytest.raises(RuntimeError, match="load failed"):
        await cache.get("db", load)
    assert load.calls == 1

    load.fail = None
    cache._states["db"].failed_at -= cache.retry_seconds
    assert await _value(cache, "db", load) == "result-2"


async def test_a_failure_without_a_message_is_still_reported():
    """asyncpg's timeout is a bare TimeoutError(), whose str() is empty."""
    cache, load = _cache(), _Loader()
    await cache.get("db", load)
    _stale(cache)
    load.fail = asyncio.TimeoutError()
    await cache.get("db", load)
    await _settle()
    served = await cache.get("db", load)
    assert (served.refreshing, served.refresh_error) == (False, "TimeoutError")


async def test_a_database_timeout_in_the_first_computation_is_a_failure_not_a_wait():
    """The computation's own TimeoutError must not read as "still running"."""
    cache, load = _cache(), _Loader()
    load.fail = asyncio.TimeoutError()
    with pytest.raises(asyncio.TimeoutError):
        await cache.get("db", load)


async def test_a_first_computation_that_outlasts_the_wait_returns_nothing_and_goes_on():
    cache, load = _cache(first_wait=0.01), _Loader()
    load.gate = asyncio.Event()

    served = await cache.get("db", load)
    assert (served.value, served.refreshing) == (None, True)
    # Asking again neither waits longer than the limit nor starts another.
    assert await _value(cache, "db", load) is None
    assert load.calls == 1

    load.gate.set()
    await _settle()
    assert await _value(cache, "db", load) == "result-1"


async def test_concurrent_first_requests_share_one_computation():
    cache, load = _cache(), _Loader()
    load.gate = asyncio.Event()
    waiters = [asyncio.create_task(_value(cache, "db", load)) for _ in range(5)]
    await _settle()
    assert load.calls == 1
    load.gate.set()
    assert await asyncio.gather(*waiters) == ["result-1"] * 5


async def test_a_waiter_that_goes_away_does_not_cancel_the_computation():
    """A client disconnecting must not cost the other waiters their result."""
    cache, load = _cache(), _Loader()
    load.gate = asyncio.Event()
    leaving = asyncio.create_task(cache.get("db", load))
    staying = asyncio.create_task(_value(cache, "db", load))
    await _settle()
    leaving.cancel()
    await _settle()
    load.gate.set()
    assert await staying == "result-1"
    assert load.calls == 1


async def test_each_key_keeps_its_own_result():
    cache = _cache()
    a, b = _Loader("a"), _Loader("b")
    assert await _value(cache, "db-a", a) == "a-1"
    assert await _value(cache, "db-b", b) == "b-1"
    assert await _value(cache, "db-a", a) == "a-1"
    assert (a.calls, b.calls) == (1, 1)


async def test_a_key_restoring_its_stored_result_never_gets_another_keys():
    """db-a's store read is still pending when db-b restores its own result."""
    store = _Store()
    store.rows["db-a"] = ("stored-a", _ago(10), 0)
    store.rows["db-b"] = ("stored-b", _ago(10), 0)
    store.read_gate["db-a"] = asyncio.Event()
    cache = _cache(store)

    reading_a = asyncio.create_task(_value(cache, "db-a", _Loader("a")))
    await _settle()
    assert await _value(cache, "db-b", _Loader("b")) == "stored-b"
    store.read_gate["db-a"].set()
    assert await reading_a == "stored-a"


async def test_each_result_is_stored_with_when_and_under_which_generation_it_was_computed():
    store = _Store()
    cache, load = _cache(store), _Loader()
    before = datetime.now(timezone.utc)
    await cache.get("db", load)
    value, computed_at, generation = store.rows["db"]
    assert (value, generation) == ("result-1", 0)
    assert before <= computed_at <= datetime.now(timezone.utc)


async def test_a_new_process_serves_the_stored_result_without_computing():
    store = _Store()
    store.rows["db"] = ("stored", _ago(10), 0)
    cache, load = _cache(store), _Loader()

    assert await _value(cache, "db", load) == "stored"
    assert await _value(cache, "db", load) == "stored"
    assert load.calls == 0
    # Read once, not on every request.
    assert store.reads == 1


async def test_a_stale_stored_result_is_served_while_it_is_refreshed():
    store = _Store()
    store.rows["db"] = ("stored", _ago(600), 0)
    cache, load = _cache(store), _Loader()
    load.gate = asyncio.Event()

    served = await cache.get("db", load)
    assert (served.value, served.refreshing) == ("stored", True)
    load.gate.set()
    await _settle()
    assert await _value(cache, "db", load) == "result-1"
    assert store.rows["db"][0] == "result-1"


async def test_a_delete_drops_the_result_held_in_memory():
    store = _Store()
    cache, load = _cache(store), _Loader()
    assert await _value(cache, "db", load) == "result-1"

    store.delete()

    # Computed over rows that may be gone: recomputed, never served again.
    assert await _value(cache, "db", load) == "result-2"
    assert store.rows["db"][2] == 1


async def test_a_computation_a_delete_overtook_publishes_nothing_and_computes_again():
    """Rows read before the delete must reach neither memory nor the store."""
    store = _Store()
    cache, load = _cache(store), _Loader()
    load.gate = asyncio.Event()
    waiting = asyncio.create_task(_value(cache, "db", load))
    await _settle()
    assert load.calls == 1

    store.delete()  # while computation 1 holds rows read before it
    load.gate.set()

    assert await waiting == "result-2"
    assert load.calls == 2
    assert store.rows["db"] == (store.rows["db"][0], store.rows["db"][1], 1)
    assert store.rows["db"][0] == "result-2"


async def test_a_request_after_a_delete_never_gets_a_result_stored_just_before_it():
    """The computation stored before the delete committed, and reaches memory after.

    A request that began after the delete waits for that computation; it must
    not return its result, but compute again.
    """
    store = _Store()
    store.after_write = asyncio.Event()
    cache, load = _cache(store), _Loader()
    before = asyncio.create_task(_value(cache, "db", load))
    await _settle()
    assert store.rows["db"][0] == "result-1"  # stored, not yet in memory

    store.delete()
    after = asyncio.create_task(_value(cache, "db", load))
    await _settle()
    store.after_write.set()

    assert await after == "result-2"
    assert load.calls == 2
    # The request that began before the delete may see either; memory and store
    # end on the result computed after it.
    await before
    assert await _value(cache, "db", load) == "result-2"
    assert store.rows["db"][0] == "result-2"


async def test_a_request_after_a_delete_never_gets_a_refresh_stored_just_before_it():
    """With a result already kept, the request after the delete reads the store.

    The refresh stored its result, then the delete committed; the new request
    drops the kept result and, while it reads the store, the refresh finishes.
    The request must not return either, but compute again.
    """
    store = _Store()
    cache, load = _cache(store), _Loader()
    assert await _value(cache, "db", load) == "result-1"
    _stale(cache)
    store.after_write = asyncio.Event()
    assert await _value(cache, "db", load) == "result-1"  # starts the refresh
    await _settle()
    assert store.rows["db"][0] == "result-2"  # stored, not yet in memory

    store.delete()
    store.read_gate["db"] = asyncio.Event()
    after = asyncio.create_task(_value(cache, "db", load))
    await _settle()  # it has seen the delete and is reading the store
    store.after_write.set()
    await _settle()  # the refresh finishes meanwhile
    store.read_gate["db"].set()

    assert await after == "result-3"
    assert await _value(cache, "db", load) == "result-3"


async def test_a_result_published_after_a_delete_no_request_has_seen_yet_is_dropped():
    """No request saw the delete when the refresh finished: the next one drops it."""
    store = _Store()
    cache, load = _cache(store), _Loader()
    assert await _value(cache, "db", load) == "result-1"
    _stale(cache)
    store.after_write = asyncio.Event()
    await cache.get("db", load)
    await _settle()
    store.delete()
    store.after_write.set()
    await _settle()  # result-2, computed before the delete, reached memory

    assert await _value(cache, "db", load) == "result-3"


async def test_a_stored_result_from_before_a_delete_is_not_restored():
    store = _Store()
    store.rows["db"] = ("stored", _ago(10), 0)
    store.current = 1  # a delete committed, and its clear raced the row (defensive)
    cache, load = _cache(store), _Loader()
    assert await _value(cache, "db", load) == "result-1"


async def test_an_unreadable_store_falls_back_to_computing(caplog):
    store = _Store()
    store.fail_read = True
    cache, load = _cache(store), _Loader()
    with caplog.at_level(logging.WARNING):
        assert await _value(cache, "db", load) == "result-1"
    assert "could not read the stored result" in caplog.text


async def test_an_unwritable_store_still_keeps_the_result_in_memory(caplog):
    store = _Store()
    store.fail_write = True
    cache, load = _cache(store), _Loader()
    with caplog.at_level(logging.WARNING):
        assert await _value(cache, "db", load) == "result-1"
    assert "could not store the result" in caplog.text
    assert await _value(cache, "db", load) == "result-1"
    assert load.calls == 1


async def test_warm_up_fills_the_cache_and_only_logs_a_failure(caplog):
    cache, load = _cache(), _Loader()
    load.fail = RuntimeError("load failed")
    with caplog.at_level(logging.ERROR):
        await cache.warm("db", load)
    assert "refresh failed (no result yet)" in caplog.text

    load.fail = None
    await cache.warm("db", load)
    assert await _value(cache, "db", load) == "result-2"
    assert load.calls == 2


async def test_warm_up_keeps_a_fresh_stored_result_and_refreshes_a_stale_one():
    store = _Store()
    store.rows["db"] = ("stored", _ago(10), 0)
    cache, load = _cache(store), _Loader()
    await cache.warm("db", load)
    assert load.calls == 0
    assert await _value(cache, "db", load) == "stored"

    stale_store = _Store()
    stale_store.rows["db"] = ("stored", _ago(600), 0)
    cache, load = _cache(stale_store), _Loader()
    # Waits for the refresh, so warm-ups run one after another.
    await cache.warm("db", load)
    assert load.calls == 1
    served = await cache.get("db", load)
    assert (served.value, served.refreshing) == ("result-1", False)
