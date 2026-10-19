"""One expensive admin result per process, served while it refreshes.

Some admin views are exact statistics over weeks of ``api_logs`` -- percentiles,
or the newest points per series -- which no rollup can add up and which take
seconds on a production-sized table. Their data does not need to be fresher
than a few minutes, and the view says how old it is. So each is computed once,
kept, and served from memory:

- the first request in a process waits for the result, as does a startup warm-up
  (:meth:`RefreshingResult.warm`), which normally gets there first;
- a fresh result is returned as is;
- a stale one is returned at once, and one refresh starts in the background;
- an explicit refresh waits for a new result;
- concurrent callers share one computation, never start their own;
- a failed refresh keeps the previous result, which is still returned; only a
  first computation that fails reaches the caller.

The result is per process and keyed (by the database pool), so a different
database never sees another's numbers.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Generic, TypeVar

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable

logger = get_logger(__name__)

T = TypeVar("T")


class RefreshingResult(Generic[T]):
    """A result computed by a caller-supplied loader; see the module docstring."""

    def __init__(self, name: str, *, fresh_seconds: float) -> None:
        self.name = name
        self.fresh_seconds = fresh_seconds
        self._key: Hashable | None = None
        self._value: T | None = None
        self._computed_at = 0.0
        self._task: asyncio.Task[T] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def clear(self) -> None:
        """Forget the result (tests, or a changed database)."""
        self._key = None
        self._value = None
        self._computed_at = 0.0
        self._task = None

    async def get(
        self, key: Hashable, load: Callable[[], Awaitable[T]], *, refresh: bool = False
    ) -> T:
        """The result for *key*; see the module docstring for when *load* runs."""
        self._bind_loop()
        if key != self._key:
            self.clear()
            self._key = key
        # This key's result as the request starts: the only fallback this
        # caller may get, as another key can take over the cache meanwhile.
        previous = self._value
        if previous is None or refresh:
            return await self._wait_for_refresh(load, previous)
        if time.monotonic() - self._computed_at >= self.fresh_seconds:
            self._start(load)
        return self._value

    async def warm(self, key: Hashable, load: Callable[[], Awaitable[T]]) -> None:
        """Compute the result ahead of the first request; failures are only logged."""
        try:
            await self.get(key, load)
        except Exception:
            logger.exception(f"{self.name}: warm-up failed")

    async def _wait_for_refresh(self, load: Callable[[], Awaitable[T]], previous: T | None) -> T:
        task = self._start(load)
        try:
            # Shielded: a client that disconnects must not cancel the shared
            # computation other callers are waiting on.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            if previous is not None:
                return previous
            raise

    def _start(self, load: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(load, self._key))
            self._task.add_done_callback(self._log_failure)
        return self._task

    async def _run(self, load: Callable[[], Awaitable[T]], key: Hashable | None) -> T:
        started = time.monotonic()
        value = await load()
        # A change of key while this ran means the result belongs to the old one.
        if key == self._key:
            self._value = value
            self._computed_at = time.monotonic()
        logger.info(f"{self.name}: computed in {int((time.monotonic() - started) * 1000)} ms")
        return value

    def _log_failure(self, task: asyncio.Task[T]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            kept = "keeping the previous result" if self._value is not None else "no result yet"
            logger.error(f"{self.name}: refresh failed ({kept}): {exc!r}")

    def _bind_loop(self) -> None:
        # A task belongs to the loop that made it; tests run each on a new loop.
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._task = None
