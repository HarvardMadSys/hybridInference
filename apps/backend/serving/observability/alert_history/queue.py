"""Bounded in-memory buffer and the one background writer that drains it.

The queue holds every record not yet committed, **including the batch the
writer is writing right now**: a record leaves only once its transaction has
committed (or it was found unwritable on its own), so the bounds cover
in-flight data and a retry never needs a second copy. ``offer`` never waits;
when either bound would be exceeded it refuses the new record and counts the
refusal, keeping what is already queued. Refusals are reported later as a
``history_gap`` record, best effort.

Only one writer task exists per process, so a failing database costs one
connection retrying with backoff, never a growing pile of tasks.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from serving.observability.alert_history.records import HistoryRecord

#: Diagnostics for the history path itself. Never fed back into history: the
#: capture hooks are called from the alert code, not from logging.
diag = logging.getLogger("serving.alert_history.diagnostics")

DEFAULT_MAX_RECORDS = 2000
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
BATCH_MAX_RECORDS = 50
BATCH_MAX_BYTES = 256 * 1024
FLUSH_INTERVAL_SEC = 1.0
BACKOFF_MIN_SEC = 1.0
BACKOFF_MAX_SEC = 30.0
BATCH_TIMEOUT_SEC = 15.0
HEARTBEAT_INTERVAL_SEC = 30.0
DRAIN_BUDGET_SEC = 5.0


@dataclass
class _Refusals:
    """Records refused since the last gap report, by reason."""

    counts: collections.Counter[str] = field(default_factory=collections.Counter)
    first_at: float | None = None
    last_at: float | None = None

    def add(self, reason: str, now: float) -> None:
        self.counts[reason] += 1
        if self.first_at is None:
            self.first_at = now
        self.last_at = now

    def total(self) -> int:
        return sum(self.counts.values())


class HistoryQueue:
    """FIFO of uncommitted records, bounded by count and serialized bytes."""

    def __init__(
        self,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_records = max_records
        self.max_bytes = max_bytes
        self._clock = clock
        self._items: collections.deque[HistoryRecord] = collections.deque()
        self._bytes = 0
        self._in_flight = 0
        self._closed = False
        self._refusals = _Refusals()
        #: Lifetime totals, for the status endpoint and the source snapshot.
        self.refused_total = 0
        self.dropped_total = 0
        self.committed_total = 0
        self._wakeup: asyncio.Event | None = None

    # -- producer side ------------------------------------------------------

    def offer(self, record: HistoryRecord, *, count_refusal: bool = True) -> bool:
        """Queue ``record`` without waiting. ``True`` only means "will be tried".

        ``count_refusal=False`` is for the gap report itself, whose refusal is
        already represented by the tally it carries, and which is still taken
        after :meth:`close` so the final drain can report what shutdown lost.
        """
        now = self._clock()
        reason = None
        if self._closed and count_refusal:
            reason = "after_shutdown"
        elif len(self._items) >= self.max_records or self._bytes + record.size > self.max_bytes:
            reason = "buffer_full"
        if reason is not None:
            if count_refusal:
                self._refuse(reason, now)
            return False
        self._items.append(record)
        self._bytes += record.size
        if self._wakeup is not None:
            self._wakeup.set()
        return True

    def _refuse(self, reason: str, now: float) -> None:
        self._refusals.add(reason, now)
        self.refused_total += 1
        if self.refused_total in (1, 10, 100) or self.refused_total % 1000 == 0:
            diag.warning(
                "alert history buffer refused a record: reason=%s refused_total=%d queued=%d",
                reason,
                self.refused_total,
                len(self._items),
            )

    def close(self) -> None:
        """Refuse every later offer; what is queued stays for the final drain."""
        self._closed = True

    # -- writer side --------------------------------------------------------

    def bind_wakeup(self, event: asyncio.Event) -> None:
        """Wake the writer when a record arrives."""
        self._wakeup = event
        if self._items:
            event.set()

    def peek_batch(
        self, *, max_records: int = BATCH_MAX_RECORDS, max_bytes: int = BATCH_MAX_BYTES
    ) -> list[HistoryRecord]:
        """The next batch from the head, still counted against the bounds."""
        batch: list[HistoryRecord] = []
        size = 0
        for record in self._items:
            if batch and (len(batch) >= max_records or size + record.size > max_bytes):
                break
            batch.append(record)
            size += record.size
        self._in_flight = len(batch)
        return batch

    def settle(self, batch: Sequence[HistoryRecord], *, dropped: int = 0) -> None:
        """Release a batch whose transaction committed. ``dropped`` were unwritable."""
        for record in batch:
            if not self._items or self._items[0] is not record:
                # Only the writer removes records, and only from the head, so
                # this cannot happen; refuse to guess which record left.
                raise RuntimeError("alert history queue head changed under the writer")
            self._items.popleft()
            self._bytes -= record.size
        self._in_flight = 0
        self.committed_total += len(batch) - dropped
        self.dropped_total += dropped

    def release_in_flight(self) -> None:
        """The batch failed and stays queued for the next attempt."""
        self._in_flight = 0

    def take_refusals(self) -> tuple[collections.Counter[str], float | None, float | None]:
        """Hand over the refusal tally since the last gap report and reset it."""
        refusals = self._refusals
        self._refusals = _Refusals()
        return refusals.counts, refusals.first_at, refusals.last_at

    def restore_refusals(
        self, counts: collections.Counter[str], first_at: float | None, last_at: float | None
    ) -> None:
        """Put a tally back whose gap record could not be queued."""
        for reason, count in counts.items():
            self._refusals.counts[reason] += count
        if first_at is not None and (
            self._refusals.first_at is None or first_at < self._refusals.first_at
        ):
            self._refusals.first_at = first_at
        if last_at is not None and (
            self._refusals.last_at is None or last_at > self._refusals.last_at
        ):
            self._refusals.last_at = last_at

    # -- introspection ------------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    @property
    def queued_bytes(self) -> int:
        """Serialized bytes held, in-flight batch included."""
        return self._bytes

    @property
    def in_flight(self) -> int:
        """Records of the batch currently being written."""
        return self._in_flight

    def oldest_queued_at(self) -> Any:
        """``occurred_at`` of the oldest uncommitted record, or ``None``."""
        return self._items[0].occurred_at if self._items else None

    def after_in_flight(self) -> tuple[int, int, Any]:
        """Count, bytes and oldest ``occurred_at`` of what outlives the in-flight batch.

        All three describe the same records: those still queued once the
        batch being written commits.
        """
        rest = list(itertools.islice(self._items, self._in_flight, None))
        return (
            len(rest),
            sum(record.size for record in rest),
            rest[0].occurred_at if rest else None,
        )

    def pending_refusals(self) -> int:
        """Refusals not yet reported as a gap."""
        return self._refusals.total()


class BatchWriter(Protocol):
    """What the writer needs from storage. Raises on anything retryable."""

    async def write_batch(self, records: Sequence[HistoryRecord], heartbeat: dict[str, Any]) -> int:
        """Commit ``records`` and the source heartbeat; return how many were unwritable."""
        ...

    async def heartbeat(self, heartbeat: dict[str, Any]) -> None:
        """Commit the source heartbeat alone."""
        ...


class HistoryWriter:
    """Drains a :class:`HistoryQueue` into storage until stopped."""

    def __init__(
        self,
        queue: HistoryQueue,
        storage: BatchWriter,
        *,
        heartbeat: Callable[[], dict[str, Any]],
        before_batch: Callable[[float], None] | None = None,
        gap_record: Callable[[collections.Counter[str], float | None, float | None], Any]
        | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        batch_timeout_sec: float = BATCH_TIMEOUT_SEC,
        flush_interval_sec: float = FLUSH_INTERVAL_SEC,
        heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC,
    ) -> None:
        self._queue = queue
        self._storage = storage
        self._heartbeat = heartbeat
        self._before_batch = before_batch
        self._gap_record = gap_record
        self._clock = clock
        self._sleep = sleep
        self._batch_timeout = batch_timeout_sec
        self._flush_interval = flush_interval_sec
        self._heartbeat_interval = heartbeat_interval_sec
        self._wakeup = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._backoff = 0.0
        self._last_heartbeat = 0.0
        #: For the status endpoint and the source row.
        self.last_success_at: float | None = None
        self.last_error_code: str | None = None
        self.last_error_at: float | None = None
        self.consecutive_failures = 0
        queue.bind_wakeup(self._wakeup)

    def start(self) -> None:
        """Start the writer task once."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="alert-history-writer")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._flush_interval)
            self._wakeup.clear()
            if self._stopping.is_set():
                break
            await self.flush_once()
            if self._backoff:
                delay = self._backoff * random.uniform(0.5, 1.0)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    async def flush_once(self, *, timeout: float | None = None) -> bool:
        """Write one batch (or a heartbeat when idle). ``True`` when it committed.

        ``timeout`` caps the storage call below the usual batch timeout; the
        final drain passes what is left of its budget.
        """
        limit = self._batch_timeout if timeout is None else min(self._batch_timeout, timeout)
        now = self._clock()
        if self._before_batch is not None:
            try:
                self._before_batch(now)
            except Exception:
                diag.debug("alert history pre-batch hook failed", exc_info=True)
        self._queue_gap_report()
        batch = self._queue.peek_batch()
        if not batch:
            if now - self._last_heartbeat < self._heartbeat_interval:
                return False
            try:
                await bounded(self._storage.heartbeat(self._heartbeat()), limit)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed(exc)
                return False
            self._succeeded()
            return True
        try:
            dropped = await bounded(self._storage.write_batch(batch, self._heartbeat()), limit)
        except asyncio.CancelledError:
            self._queue.release_in_flight()
            raise
        except Exception as exc:
            # Commit state unknown (a lost COMMIT response looks exactly like
            # this): the batch stays queued and is resent with the same ids,
            # which the store answers as duplicates rather than new facts.
            self._queue.release_in_flight()
            self._failed(exc)
            return False
        self._queue.settle(batch, dropped=dropped)
        self._succeeded()
        if self._queue:
            self._wakeup.set()
        return True

    def _queue_gap_report(self) -> None:
        if self._gap_record is None or self._queue.pending_refusals() == 0:
            return
        counts, first_at, last_at = self._queue.take_refusals()
        try:
            record = self._gap_record(counts, first_at, last_at)
        except Exception:
            diag.debug("alert history gap record could not be built", exc_info=True)
            self._queue.restore_refusals(counts, first_at, last_at)
            return
        # The gap report competes for the same bounded space; when it does not
        # fit, the tally is kept for the next try rather than lost.
        if record is None or not self._queue.offer(record, count_refusal=False):
            self._queue.restore_refusals(counts, first_at, last_at)

    def _succeeded(self) -> None:
        now = self._clock()
        self.last_success_at = now
        self._last_heartbeat = now
        self._backoff = 0.0
        self.consecutive_failures = 0

    def _failed(self, exc: BaseException) -> None:
        self.consecutive_failures += 1
        self.last_error_code = _error_code(exc)
        self.last_error_at = self._clock()
        self._backoff = min(
            BACKOFF_MAX_SEC, max(BACKOFF_MIN_SEC, self._backoff * 2 or BACKOFF_MIN_SEC)
        )
        if self.consecutive_failures in (1, 5) or self.consecutive_failures % 50 == 0:
            diag.warning(
                "alert history write failed: error=%s consecutive=%d queued=%d",
                self.last_error_code,
                self.consecutive_failures,
                len(self._queue),
            )

    async def stop(self, *, drain_budget_sec: float = DRAIN_BUDGET_SEC) -> int:
        """Stop the loop and drain, all within ``drain_budget_sec``. Returns records left.

        The budget starts now and covers everything: the write the loop may
        already be waiting on, cancelling it, and the drain. A database call
        that hangs therefore costs the budget, not the batch timeout.
        """
        deadline = time.monotonic() + max(0.0, drain_budget_sec)
        self._stopping.set()
        self._wakeup.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - time.monotonic()))
            if not done:
                task.cancel()
                await asyncio.wait({task}, timeout=CANCEL_GRACE_SEC)
        if task is not None and task.done() and not task.cancelled() and task.exception():
            diag.debug("alert history writer ended with an error", exc_info=task.exception())
        while self._queue or self._queue.pending_refusals():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                committed = await self.flush_once(timeout=remaining)
            except asyncio.CancelledError:
                break
            if not committed:
                # A failed attempt in the drain is not retried at backoff
                # speed, nor in a tight loop; wait a little, inside the budget.
                await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        left = len(self._queue)
        if left:
            diag.warning(
                "alert history shutdown left %d uncommitted record(s); they are lost", left
            )
        return left


#: How long a cancelled storage call may take to unwind before it is abandoned.
CANCEL_GRACE_SEC = 0.1


async def bounded(coro: Awaitable[Any], timeout: float) -> Any:
    """Await ``coro`` for at most ``timeout`` (+ a short cancel grace).

    Unlike ``asyncio.wait_for`` this never waits for a cancelled call to finish
    unwinding past :data:`CANCEL_GRACE_SEC`, so a hung connection cannot extend
    a shutdown. Cancelling the caller cancels the call too.
    """
    task = asyncio.ensure_future(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
    except BaseException:
        task.cancel()
        raise
    if not done:
        task.cancel()
        await asyncio.wait({task}, timeout=CANCEL_GRACE_SEC)
        raise TimeoutError("alert history storage call timed out")
    return task.result()


def _error_code(exc: BaseException) -> str:
    """A stable, non-free-text code for an exception."""
    name = type(exc).__name__
    code = "".join(ch if ch.isalnum() else "_" for ch in name).lower()
    return f"db:{code}"[:100]
