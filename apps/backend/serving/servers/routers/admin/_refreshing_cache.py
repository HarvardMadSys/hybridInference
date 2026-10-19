"""One expensive admin result per database, kept, and refreshed in the background.

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
  :attr:`Served.refreshing` says one is running, and the console polls until it
  is not;
- only when there is no result at all does a request wait -- for at most
  ``first_wait_seconds`` (below the console proxy's timeout), after which it
  gets no value and the computation goes on;
- concurrent callers share one computation, never start their own;
- a failed computation keeps the previous result and is reported
  (:attr:`Served.refresh_error`); a stale result is not retried for
  ``retry_seconds``, so polling ends and a failing query is not rerun on every
  poll. With no result to keep, the failure reaches the caller instead.

Results are computed over rows a user's hard delete removes. The store keeps a
*generation* the delete advances in its own transaction. A computation reads it
before it starts and publishes only if it is unchanged -- checked atomically
with the store's write -- so a computation that read rows before a delete never
publishes them, and computes again. A result held in memory is dropped once the
generation moves past the one it was computed under.

State is kept per key (the database pool): a request only ever sees its own
key's result, whatever another key does meanwhile.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable

logger = get_logger(__name__)

T = TypeVar("T")

# Keys kept: one database pool in production; a few in tests.
_MAX_KEYS = 4
# Times a computation is redone when a delete supersedes it before it publishes.
_MAX_ATTEMPTS = 3


class ResultStore(Protocol[T]):
    """Where a result outlives the process, and the generation deletes advance."""

    async def generation(self, key: Hashable) -> int:
        """The current generation for *key*."""

    async def read(self, key: Hashable) -> tuple[T, datetime, int] | None:
        """The stored result for *key*, when it was computed, and its generation."""

    async def write(self, key: Hashable, value: T, computed_at: datetime, generation: int) -> bool:
        """Store *value* if *generation* is still current; whether it was stored."""


@dataclass
class Served(Generic[T]):
    """What a request gets: the result, if any, and the state of its refresh."""

    value: T | None
    refreshing: bool
    refresh_error: str | None


class _Superseded(Exception):
    """A delete advanced the generation while a computation ran."""


@dataclass
class _State(Generic[T]):
    value: T | None = None
    computed_at: float = 0.0  # wall clock, as stored results carry it
    generation: int = 0
    restored: bool = False
    task: asyncio.Task[T] | None = None
    error: Exception | None = None  # the last failure, cleared by a success
    failed_at: float = 0.0


class RefreshingResult(Generic[T]):
    """A result computed by a caller-supplied loader; see the module docstring."""

    def __init__(
        self,
        name: str,
        *,
        fresh_seconds: float,
        first_wait_seconds: float,
        retry_seconds: float,
        store: ResultStore[T] | None = None,
    ) -> None:
        self.name = name
        self.fresh_seconds = fresh_seconds
        self.first_wait_seconds = first_wait_seconds
        self.retry_seconds = retry_seconds
        self.store = store
        self._states: OrderedDict[Hashable, _State[T]] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None

    def clear(self) -> None:
        """Forget every result (tests)."""
        self._states.clear()

    async def get(
        self, key: Hashable, load: Callable[[], Awaitable[T]], *, refresh: bool = False
    ) -> Served[T]:
        """The result for *key*; see the module docstring for when *load* runs."""
        state = self._state(key)
        await self._check(key, state)
        if state.value is None:
            if not refresh and self._recently_failed(state) and not self._running(state):
                raise state.error  # type: ignore[misc]
            await self._wait_for_first(self._start(key, state, load))
        elif refresh or (self._stale(state) and not self._recently_failed(state)):
            self._start(key, state, load)
        return Served(
            value=state.value,
            refreshing=self._running(state),
            refresh_error=str(state.error) if state.error is not None else None,
        )

    async def warm(self, key: Hashable, load: Callable[[], Awaitable[T]]) -> None:
        """Have a fresh result ahead of the first request; failures are only logged.

        Waits for the computation, if one is needed, so that warm-ups run one
        after another rather than all at once.
        """
        try:
            state = self._state(key)
            await self._check(key, state)
            if state.value is None or self._stale(state):
                await asyncio.wait({self._start(key, state, load)})
        except Exception:
            logger.exception(f"{self.name}: warm-up failed")

    def _state(self, key: Hashable) -> _State[T]:
        # A task belongs to the loop that made it; tests run each on a new loop.
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            for state in self._states.values():
                state.task = None
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _State()
            while len(self._states) > _MAX_KEYS:
                self._states.popitem(last=False)
        self._states.move_to_end(key)
        return state

    async def _check(self, key: Hashable, state: _State[T]) -> None:
        """Drop a result a delete has superseded, then adopt the stored one if none."""
        if self.store is None:
            return
        try:
            current = await self.store.generation(key)
        except Exception as exc:
            logger.warning(f"{self.name}: could not read the generation: {exc!r}")
            return
        if state.value is not None and state.generation != current:
            state.value, state.computed_at, state.restored = None, 0.0, False
        if state.restored or state.value is not None:
            return
        state.restored = True
        try:
            stored = await self.store.read(key)
        except Exception as exc:
            logger.warning(f"{self.name}: could not read the stored result: {exc!r}")
            return
        # A computation may have published meanwhile; it is at least as new.
        if stored is not None and state.value is None and stored[2] == current:
            state.value, computed_at, state.generation = stored
            state.computed_at = computed_at.timestamp()

    def _stale(self, state: _State[T]) -> bool:
        return time.time() - state.computed_at >= self.fresh_seconds

    def _recently_failed(self, state: _State[T]) -> bool:
        return state.error is not None and time.time() - state.failed_at < self.retry_seconds

    @staticmethod
    def _running(state: _State[T]) -> bool:
        return state.task is not None and not state.task.done()

    async def _wait_for_first(self, task: asyncio.Task[T]) -> None:
        # asyncio.wait neither cancels the shared computation when this request
        # goes away or runs out of time, nor turns the computation's own
        # TimeoutError -- a database timeout -- into "still running".
        done, _ = await asyncio.wait({task}, timeout=self.first_wait_seconds)
        if done:
            task.result()

    def _start(
        self, key: Hashable, state: _State[T], load: Callable[[], Awaitable[T]]
    ) -> asyncio.Task[T]:
        if state.task is None or state.task.done():
            state.task = asyncio.get_running_loop().create_task(self._run(key, state, load))
            state.task.add_done_callback(_retrieve)
        return state.task

    async def _run(self, key: Hashable, state: _State[T], load: Callable[[], Awaitable[T]]) -> T:
        started = time.monotonic()
        try:
            for _ in range(_MAX_ATTEMPTS):
                generation = await self.store.generation(key) if self.store is not None else 0
                value = await load()
                computed_at = datetime.now(timezone.utc)
                if await self._publish(key, value, computed_at, generation):
                    state.value, state.generation = value, generation
                    state.computed_at = computed_at.timestamp()
                    state.error = None
                    logger.info(
                        f"{self.name}: computed in {int((time.monotonic() - started) * 1000)} ms"
                    )
                    return value
                logger.info(f"{self.name}: a delete superseded the computation; computing again")
            raise _Superseded(f"superseded by deletes {_MAX_ATTEMPTS} times")
        except Exception as exc:
            state.error, state.failed_at = exc, time.time()
            kept = "keeping the previous result" if state.value is not None else "no result yet"
            logger.error(f"{self.name}: refresh failed ({kept}): {exc!r}")
            raise

    async def _publish(
        self, key: Hashable, value: T, computed_at: datetime, generation: int
    ) -> bool:
        """Store *value* unless a delete moved the generation; whether to keep it."""
        if self.store is None:
            return True
        try:
            return await self.store.write(key, value, computed_at, generation)
        except Exception as exc:
            logger.warning(f"{self.name}: could not store the result: {exc!r}")
        # Not stored, but still the caller's to keep while no delete intervened.
        return await self.store.generation(key) == generation


def _retrieve(task: asyncio.Task) -> None:
    """Mark a finished task's exception retrieved; :meth:`_run` already logged it."""
    if not task.cancelled():
        task.exception()
