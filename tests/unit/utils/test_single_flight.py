"""SingleFlightCache: one computation per key at a time, kept for a short TTL."""

from __future__ import annotations

import asyncio

import pytest

from serving.utils.single_flight import SingleFlightCache

pytestmark = pytest.mark.asyncio


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Counter:
    """A computation that counts its runs and can be held open."""

    def __init__(self) -> None:
        self.runs = 0
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self) -> int:
        self.runs += 1
        await self.release.wait()
        return self.runs


async def test_a_value_is_reused_within_the_ttl_and_recomputed_after():
    clock = _Clock()
    cache: SingleFlightCache[int] = SingleFlightCache(5.0, clock=clock)
    compute = _Counter()

    assert await cache.get("k", compute) == 1
    clock.now += 4.9
    assert await cache.get("k", compute) == 1
    clock.now += 0.2
    assert await cache.get("k", compute) == 2
    assert compute.runs == 2


async def test_concurrent_callers_share_one_computation():
    cache: SingleFlightCache[int] = SingleFlightCache(5.0)
    compute = _Counter()
    compute.release.clear()

    waiters = [asyncio.ensure_future(cache.get("k", compute)) for _ in range(50)]
    await asyncio.sleep(0)
    compute.release.set()

    assert await asyncio.gather(*waiters) == [1] * 50
    assert compute.runs == 1


async def test_keys_are_computed_separately():
    cache: SingleFlightCache[str] = SingleFlightCache(5.0)

    async def value_of(key: str) -> str:
        return key.upper()

    assert await cache.get("a", lambda: value_of("a")) == "A"
    assert await cache.get("b", lambda: value_of("b")) == "B"


async def test_a_failure_reaches_every_waiter_and_is_not_cached():
    cache: SingleFlightCache[int] = SingleFlightCache(5.0)
    release = asyncio.Event()
    runs = 0

    async def failing() -> int:
        nonlocal runs
        runs += 1
        await release.wait()
        raise RuntimeError("database down")

    waiters = [asyncio.ensure_future(cache.get("k", failing)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert [type(r) for r in results] == [RuntimeError] * 3
    assert runs == 1

    async def working() -> int:
        return 7

    assert await cache.get("k", working) == 7


async def test_a_cancelled_caller_does_not_cancel_the_others():
    cache: SingleFlightCache[int] = SingleFlightCache(5.0)
    compute = _Counter()
    compute.release.clear()

    first = asyncio.ensure_future(cache.get("k", compute))
    second = asyncio.ensure_future(cache.get("k", compute))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    compute.release.set()

    assert await second == 1
    assert first.cancelled()
    # And the value it computed is cached.
    assert await cache.get("k", compute) == 1
    assert compute.runs == 1


async def test_the_oldest_value_goes_past_max_entries():
    cache: SingleFlightCache[int] = SingleFlightCache(60.0, max_entries=2)
    compute = _Counter()

    await cache.get("a", compute)
    await cache.get("b", compute)
    await cache.get("c", compute)

    assert await cache.get("b", compute) == 2
    assert await cache.get("c", compute) == 3
    assert await cache.get("a", compute) == 4


async def test_object_keys_are_compared_by_identity():
    cache: SingleFlightCache[int] = SingleFlightCache(60.0, max_entries=1)
    compute = _Counter()
    first, second = object(), object()

    assert await cache.get((first,), compute) == 1
    assert await cache.get((second,), compute) == 2
    assert await cache.get((second,), compute) == 2


async def test_invalid_settings_are_refused():
    with pytest.raises(ValueError):
        SingleFlightCache(0)
    with pytest.raises(ValueError):
        SingleFlightCache(1.0, max_entries=0)
