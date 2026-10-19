"""One expensive admin result per process, kept, and refreshed in the background.

Some admin views are exact statistics over weeks of ``api_logs`` -- percentiles,
or the newest points per series -- which no rollup can add up and which take
seconds to minutes on a production-sized table. Their data does not need to be
fresher than a few minutes, and the view says how old it is. So how long a page
takes to open must not depend on how long the computation takes:

- a result is kept in memory and, through an optional :class:`ResultStore`, in
  the database, so a restarted process serves the last one at once;
- a fresh result is returned as is;
- a stale one is returned at once, and one refresh starts in the background;
- an explicit refresh also returns the current result at once and starts one;
  :attr:`RefreshingResult.refreshing` says one is running, and the console
  polls until it is not;
- only when there is no result at all does a request wait -- for at most
  ``first_wait_seconds`` (below the console proxy's timeout), after which it
  gets ``None`` and the computation goes on;
- concurrent callers share one computation, never start their own;
- a failed refresh keeps the previous result; only a first computation that
  fails while a request waits reaches the caller.

The result is per process and keyed (by the database pool), so a different
database never sees another's numbers.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable

logger = get_logger(__name__)

T = TypeVar("T")


class ResultStore(Protocol[T]):
    """Where a result outlives the process: read back by a process that has none."""

    async def read(self, key: Hashable) -> tuple[T, datetime] | None:
        """The stored result for *key* and when it was computed, if any."""

    async def write(self, key: Hashable, value: T, computed_at: datetime) -> None:
        """Store *value*, computed at *computed_at*, as *key*'s result."""


class RefreshingResult(Generic[T]):
    """A result computed by a caller-supplied loader; see the module docstring."""

    def __init__(
        self,
        name: str,
        *,
        fresh_seconds: float,
        first_wait_seconds: float,
        store: ResultStore[T] | None = None,
    ) -> None:
        self.name = name
        self.fresh_seconds = fresh_seconds
        self.first_wait_seconds = first_wait_seconds
        self.store = store
        self._key: Hashable | None = None
        self._value: T | None = None
        # Wall-clock time, as stored results carry it.
        self._computed_at = 0.0
        self._restored = False
        self._task: asyncio.Task[T] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def clear(self) -> None:
        """Forget the result (tests, or a changed database)."""
        self._key = None
        self._value = None
        self._computed_at = 0.0
        self._restored = False
        self._task = None

    @property
    def refreshing(self) -> bool:
        """Whether a computation is running now."""
        return self._task is not None and not self._task.done()

    async def get(
        self, key: Hashable, load: Callable[[], Awaitable[T]], *, refresh: bool = False
    ) -> T | None:
        """The result for *key*, or None while a first computation is still running.

        See the module docstring for when *load* runs and what waits for it.
        """
        self._select(key)
        await self._restore(key)
        if self._value is None:
            return await self._wait_for_first(self._start(load))
        if refresh or self._stale():
            self._start(load)
        return self._value

    async def warm(self, key: Hashable, load: Callable[[], Awaitable[T]]) -> None:
        """Have a fresh result ahead of the first request; failures are only logged.

        Waits for the computation, if one is needed, so that warm-ups run one
        after another rather than all at once.
        """
        try:
            self._select(key)
            await self._restore(key)
            if self._value is None or self._stale():
                await asyncio.shield(self._start(load))
        except Exception:
            logger.exception(f"{self.name}: warm-up failed")

    def _select(self, key: Hashable) -> None:
        # A task belongs to the loop that made it; tests run each on a new loop.
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._task = None
        if key != self._key:
            self.clear()
            self._key = key

    def _stale(self) -> bool:
        return time.time() - self._computed_at >= self.fresh_seconds

    async def _restore(self, key: Hashable) -> None:
        """Adopt the stored result, once per key, when there is none in memory."""
        if self._restored or self.store is None:
            return
        self._restored = True
        try:
            stored = await self.store.read(key)
        except Exception as exc:
            logger.warning(f"{self.name}: could not read the stored result: {exc!r}")
            return
        # Another key may have taken over, or a computation finished, meanwhile.
        if stored is not None and key == self._key and self._value is None:
            self._value, computed_at = stored
            self._computed_at = computed_at.timestamp()

    async def _wait_for_first(self, task: asyncio.Task[T]) -> T | None:
        try:
            # Shielded: neither a client that disconnects nor the time limit
            # may cancel the shared computation.
            return await asyncio.wait_for(asyncio.shield(task), self.first_wait_seconds)
        except asyncio.TimeoutError:
            return None

    def _start(self, load: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(load, self._key))
            self._task.add_done_callback(self._log_failure)
        return self._task

    async def _run(self, load: Callable[[], Awaitable[T]], key: Hashable | None) -> T:
        started = time.monotonic()
        value = await load()
        computed_at = datetime.now(timezone.utc)
        logger.info(f"{self.name}: computed in {int((time.monotonic() - started) * 1000)} ms")
        # A change of key while this ran means the result belongs to the old one.
        if key != self._key:
            return value
        self._value = value
        self._computed_at = computed_at.timestamp()
        if self.store is not None:
            try:
                await self.store.write(key, value, computed_at)
            except Exception as exc:
                logger.warning(f"{self.name}: could not store the result: {exc!r}")
        return value

    def _log_failure(self, task: asyncio.Task[T]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            kept = "keeping the previous result" if self._value is not None else "no result yet"
            logger.error(f"{self.name}: refresh failed ({kept}): {exc!r}")
