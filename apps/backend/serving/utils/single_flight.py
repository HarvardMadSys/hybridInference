"""A small expiring cache whose misses are computed once, however many callers wait.

For reads that are expensive, asked for in bursts and fine a few seconds stale:
an anonymous health probe, a user's usage totals. Within the time-to-live a
value is answered from memory; when it has lapsed, the first caller starts the
computation and everyone who asks meanwhile awaits that same one instead of
starting their own, so a burst of N callers costs one query, not N.

The computation runs as a task of its own. A caller that is cancelled (a client
that went away) stops waiting without cancelling it for the others, and a
failure is handed to everyone who was waiting and is not cached: the next
caller tries again.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable

V = TypeVar("V")


class SingleFlightCache(Generic[V]):
    """Values kept for ``ttl`` seconds after they were computed, at most ``max_entries``.

    Keys are held strongly while cached or in flight, so an object used as a
    key (a store, say) is never confused with a later one that happens to
    reuse its memory.
    """

    def __init__(
        self,
        ttl: float,
        *,
        max_entries: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an empty cache.

        Args:
            ttl: Seconds a computed value is answered from memory.
            max_entries: Values kept at most; the oldest computed goes first.
            clock: Monotonic clock, for tests.
        """
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock
        self._values: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Task[V]] = {}

    async def get(self, key: Hashable, compute: Callable[[], Awaitable[V]]) -> V:
        """Return the cached value for *key*, or the one *compute* produces.

        Only one *compute* per key runs at a time: a caller arriving while it
        runs awaits its result.
        """
        cached = self._values.get(key)
        if cached is not None and self._clock() < cached[0]:
            return cached[1]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._compute(key, compute))
            task.add_done_callback(_retrieve_exception)
            self._inflight[key] = task
        return await asyncio.shield(task)

    def clear(self) -> None:
        """Forget every cached value; a computation in flight still finishes."""
        self._values.clear()

    async def _compute(self, key: Hashable, compute: Callable[[], Awaitable[V]]) -> V:
        try:
            value = await compute()
        finally:
            # Leaves the in-flight table in the same step as the value enters
            # the cache (no await in between), so no caller ever finds neither.
            self._inflight.pop(key, None)
        self._values.pop(key, None)
        self._values[key] = (self._clock() + self._ttl, value)
        while len(self._values) > self._max_entries:
            self._values.popitem(last=False)
        return value


def _retrieve_exception(task: asyncio.Task[Any]) -> None:
    """Mark a failure as seen, for the case where every waiter was cancelled first."""
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()
