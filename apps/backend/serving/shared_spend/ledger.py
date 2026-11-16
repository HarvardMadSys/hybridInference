"""Shared spend accounting: reservations and settlements every gateway process sees.

The shared mode's counterpart of :class:`serving.spend_ledger.SpendLedger`,
with the same budget policy (:func:`serving.spend_ledger.decide_admission`)
and the same batched writer, but its state of record is PostgreSQL:

- **Admission** (:meth:`SharedSpendLedger.admit`) is one short transaction
  under the advisory locks of the scopes it checks
  (:meth:`SharedSpendStore.admit_on`): the counters and every process's open
  reservations are read in one snapshot taken after the locks were granted,
  the policy decides, and the reservation is a row before the transaction
  commits. The request is dispatched only once that row is known to exist.
  This prevents two admissions, in any processes, from being granted the
  same estimated headroom. It does not bound what a request then costs: the
  policy reserves an *estimate*, clamped to what is left and never below the
  flat minimum, and a request may cost more than its estimate.
- **Costs** recorded while a request runs are attached to its reservation
  (``req_ctx.SPEND_RESERVATION``) and counted in this process's checks at
  once wherever they exceed its estimate. When the request ends, its
  execution slot is given back at once by its owner, and the reservation is
  queued for settlement with exactly the costs its billing points recorded,
  none included: what a request costs is theirs to decide, as it is in the
  per-process mode. A request that failed, was cancelled or timed out is
  charged what they charged it and holds nothing afterwards.
- **Settlement** sends what is queued in batches, one at a time per process:
  each batch has its own id and a frozen payload, and one transaction
  (:meth:`SharedSpendStore.settle`) adds the costs to the counters and
  retires exactly its reservations, recording both. A batch whose answer was
  lost is sent again unchanged, which the recorded id makes harmless; only a
  batch the database answered with a refusal of its values is split.

What only this process knows -- costs settled to nobody yet, the part of a
request's recorded cost above its reservation, and settlements whose outcome
left a liability unaccounted -- is added to every check made here and
reported by :meth:`SharedSpendLedger.holdings`. Nothing here expires a
reservation: one whose process died before settling it stays counted in the
database until an operator reconciles it (``python -m serving.shared_spend
reconcile``), and one whose costs the counters refused stays there
quarantined.

Three things keep this accounting from needing attention nobody knows to
give:

- **Closure.** A process that shuts down with everything it sent answered
  records that it will send nothing more (:meth:`SharedSpendLedger.close`).
  That, or an operator's word for a process that was killed, is what lets its
  settlement receipts be deleted.
- **Upkeep.** About once an hour a process that is doing work deletes the
  receipts nothing can need again (its own answered ones, and those of
  processes closed long enough ago; see
  :meth:`~serving.shared_spend.store.SharedSpendStore.purge_receipts`) and
  surveys what every process holds, alerting when reservations are held by a
  process no longer heard from.
- **Numbers.** :meth:`SharedSpendLedger.stats` reports this process's
  admission latency and outcomes, what it holds and for how long, and how its
  settlements are going; a settlement failing for
  :data:`SETTLEMENT_STALLED_SECONDS`, and an admission the database did not
  answer, are alerted.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import random
import time
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from serving import quota
from serving.observability.tracked_tasks import tracked_task
from serving.shared_spend.stats import LedgerStats, survey
from serving.shared_spend.store import (
    PURGE_BATCHES,
    AdmissionRequest,
    CostLine,
    Member,
    SettlementBatch,
    SettlementResult,
    SharedSpendStore,
    SpendSnapshot,
    usd,
)
from serving.storage.pools import REQUEST_ACQUIRE_TIMEOUT_SEC, DatabaseBusyError
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from serving.spend_ledger import SpendCheck

logger = get_logger(__name__)

#: The longest one admission may take, retries, waits for a connection and for
#: locks included: the request path's existing bound for a database lookup
#: (``serving.storage.pools.REQUEST_ACQUIRE_TIMEOUT_SEC``).
ADMISSION_BUDGET_SECONDS = REQUEST_ACQUIRE_TIMEOUT_SEC

#: Pause between two attempts of one admission after a connection failure.
_ADMISSION_RETRY_PAUSE_SECONDS = 0.05

#: The most reservations one settlement batch retires.
MAX_BATCH_MEMBERS = 256

#: Retry delays after a failed settlement, doubling up to the last (as the
#: per-process writer's).
_RETRY_FIRST_SECONDS = 0.5
_RETRY_MAX_SECONDS = 30.0

#: The most marks that may fail in one pass over the reservations waiting to
#: be marked quarantined, before the rest wait for the next pass: what a
#: database that refuses every mark costs each time.
_MARK_FAILURES_PER_PASS = 8

#: How long what a settlement just added to the counters stays known to this
#: process, for an admission whose read may predate it (see
#: :meth:`SharedSpendLedger._local_extra`): as long as an admission can run,
#: and a second.
_LANDED_SECONDS = ADMISSION_BUDGET_SECONDS + 1.0

#: Settlement writes failing for this long are reported and alerted, once a stall.
SETTLEMENT_STALLED_SECONDS = 60.0

#: How often a process deletes the receipts nothing can need again and surveys
#: what every process holds; the first time at a random point within it, so
#: the processes a deploy starts together do not all do it at once.
UPKEEP_SECONDS = 3600.0

#: The most purge statements one upkeep runs.
_UPKEEP_ROUNDS = 20

#: When an upkeep stopped at that many with more to delete, the next one comes
#: this soon rather than in an hour: a busy process makes a receipt for nearly
#: every request, and its deletion must not fall behind for good.
_UPKEEP_CATCH_UP_SECONDS = 60.0

#: Stranded reservations are alerted on once the oldest is this old: a request
#: may run for minutes, and a live owner may miss its heartbeats for a moment.
STRANDED_ALERT_SECONDS = 900.0

#: The longest recording this process's closure may hold up shutdown.
_CLOSE_TIMEOUT_SECONDS = 3.0

#: This process asks for the unavailable-admission alert at most this often
#: (the alert's own cooldown is longer).
_UNAVAILABLE_ALERT_SECONDS = 60.0

#: SQLSTATEs an admission retries with the same reservation id: another
#: attempt's row just became visible (23505), a serialization failure (40).
#: A failed connection (class 08) is retried too (:func:`_retryable`).
_RETRY_ADMISSION_SQLSTATES = frozenset({"23505", "40001", "40P01"})

#: Settlement outcomes after which a member's liability is no longer held
#: anywhere this process can see: kept here, counted and reported.
_UNACCOUNTED_OUTCOMES = frozenset({"missing", "mismatch"})


class SpendAdmissionUnavailable(DatabaseBusyError):
    """The shared admission could not be completed in time; answered 503 + ``Retry-After``."""


class AccountErased(Exception):
    """The account was hard-deleted while the request was being admitted."""


class ReservationConflict(RuntimeError):
    """A reservation id was reused, or is already retired (a defect, never a client's doing)."""


class SharedReservation:
    """An admitted request's reservation, and the costs recorded under it.

    ``state``: ``active`` while the request runs; ``queued`` once it ended and
    its settlement waits for (or is in) a batch; then ``retired`` (a batch
    retired it), ``quarantined`` (the counters refused its costs) or
    ``unaccounted`` (its settlement found it no longer held as recorded here).
    ``ended_day`` is the UTC day the request ended on: the day a reservation
    that outlives its settlement keeps counting against. ``admitted_at`` and
    ``ended_at`` are this process's monotonic clock, for how long it has been
    held.
    """

    __slots__ = (
        "_ledger",
        "admitted_at",
        "amount",
        "charges",
        "check_day",
        "ended_at",
        "ended_day",
        "key_id",
        "operation",
        "project_id",
        "reservation_id",
        "state",
        "user_id",
    )

    def __init__(
        self,
        ledger: SharedSpendLedger,
        reservation_id: str,
        *,
        user_id: str,
        key_id: int | None,
        project_id: str | None,
        amount: Decimal,
        check_day: str,
        operation: str,
    ) -> None:
        """An admitted reservation of *amount*, held by *ledger*."""
        self._ledger = ledger
        self.reservation_id = reservation_id
        self.user_id = user_id
        self.key_id = key_id
        self.project_id = project_id
        self.amount = amount
        self.check_day = check_day
        self.operation = operation
        self.state = "active"
        self.charges: list[CostLine] = []
        self.ended_day: str | None = None
        self.admitted_at = time.monotonic()
        self.ended_at: float | None = None

    def owns(self, user_id: str, api_key_id: int | None, project_id: str | None) -> bool:
        """Whether a cost so attributed is this reservation's."""
        return (user_id, api_key_id, project_id) == (self.user_id, self.key_id, self.project_id)

    def charge(self, line: CostLine) -> bool:
        """Attach a cost while the request runs; False once it has ended (a late cost)."""
        if self.state != "active":
            return False
        self.charges.append(line)
        return True

    def release(self) -> None:
        """The request ended: queue the reservation for settlement. Idempotent.

        However it ended. Its costs are those its billing points recorded
        under it, any number, none included, and the rest of the estimate is
        given back: a failure, a cancellation or a timeout costs what the
        billing point charged for it (the cancellation billing prices a call
        that was cut off; a refused or failed call is charged nothing), and
        never holds the caller's budget after it. The estimate itself is never
        charged.
        """
        if self.state == "active":
            self._ledger._complete(self)

    def cost(self) -> Decimal:
        """What the costs recorded under it add up to."""
        return sum((line.cost_usd for line in self.charges), Decimal(0))

    def member(self) -> Member:
        """The settlement member this reservation is retired by."""
        return Member(
            reservation_id=self.reservation_id,
            user_id=self.user_id,
            api_key_id=self.key_id,
            project_id=self.project_id,
            kind="settle",
            costs=tuple(self.charges),
        )


@dataclasses.dataclass(slots=True)
class _Abandoned:
    """An admission that did not finish, whose transaction may still have inserted its row."""

    reservation_id: str
    user_id: str
    api_key_id: int | None
    project_id: str | None
    xids: list[int]


class SharedSpendLedger:
    """Shared reservations and settlements for one operational store, in this process."""

    shared = True

    def __init__(self, backend: SharedSpendStore, *, instance_id: str) -> None:
        """Keep *backend*'s statements, under this process's *instance_id*."""
        self._backend = backend
        self._instance_id = instance_id
        self._active: dict[str, SharedReservation] = {}
        # Members waiting for a batch, in order; their reservations until retired.
        self._queued: dict[str, Member] = {}
        self._queued_reservations: dict[str, SharedReservation] = {}
        self._increments: list[CostLine] = []
        # Admissions that did not finish: released once their transactions ended.
        self._abandoned: list[_Abandoned] = []
        # The batch being written (also in one of the two queues below), then
        # any batch to send again unchanged, and batches of one entry made by
        # splitting a refused batch.
        self._writing: SettlementBatch | None = None
        self._replay: list[SettlementBatch] = []
        self._solo: list[SettlementBatch] = []
        # What the counters refused, kept counted here and reported.
        self._set_aside: list[CostLine] = []
        # Reservations whose costs the counters refused, until their rows are
        # known to be marked quarantined: (member, the day it ended, why). And,
        # after a mark failed: when the next may be tried, the delay that led
        # there and the timer that wakes the writer for it, since when marks
        # have been failing, and whether that was reported (monotonic clock).
        self._marking: list[tuple[Member, str, str]] = []
        self._mark_after = 0.0
        self._mark_delay = 0.0
        self._mark_retry: asyncio.TimerHandle | None = None
        self._mark_failing_since: float | None = None
        self._mark_failure_reported = False
        # Reservations past settlement that still hold a liability: quarantined
        # in the database (the counters refused their costs), or no longer held
        # as recorded (unaccounted, counted here in full). One counts against
        # the day it ended on and that day's month, as a quarantined row does
        # in the database; those of an earlier month count against nothing any
        # more and are set apart, for the report only. Never forgotten by this
        # process.
        self._liabilities: dict[str, SharedReservation] = {}
        self._past_liabilities: dict[str, SharedReservation] = {}
        # What settlements answered in the last few seconds moved out of this
        # process's keeping and into the counters, each with when its answer
        # was handled: (account, key, project, day or None for today, amount).
        self._landed: collections.deque[tuple[float, tuple[tuple[Any, ...], ...]]] = (
            collections.deque()
        )
        self._writer: asyncio.Task[None] | None = None
        self._retry: asyncio.TimerHandle | None = None
        self._retry_delay = 0.0
        # Per scope key: the lock, and how many admissions use it now.
        self._locks: dict[tuple[int, int], list[Any]] = {}
        # What this process observed (stats), on its monotonic clock.
        self._stats = LedgerStats()
        self._started = time.monotonic()
        # Since when no settlement write was answered, until one is; whether
        # that stall was reported; and what the last failure was.
        self._failing_since: float | None = None
        self._stall_reported = False
        self._last_error: str | None = None
        self._unavailable_alert_at: float | None = None
        # Set by close(): this process sends nothing after it.
        self._closed = False
        # The upkeep: its task while it runs, when it is next due, and what
        # its last survey of the database found, and when.
        self._upkeep: asyncio.Task[None] | None = None
        self._upkeep_at = self._started + random.uniform(0, UPKEEP_SECONDS)
        self._survey: dict[str, Any] | None = None
        self._surveyed_at: float | None = None
        #: For tests only: awaited between the steps of an admission transaction,
        #: and before a settlement's COMMIT.
        self.pause: Callable[[str], Any] | None = None
        self.settle_pause: Callable[[str], Any] | None = None

    @property
    def instance_id(self) -> str:
        """The instance id this process's reservations are owned by."""
        return self._instance_id

    # -- what only this process knows -----------------------------------------

    def _local_extra(
        self,
        user_id: str,
        key_id: int | None,
        project_id: str | None,
        *,
        reserved: bool,
        read_from: float | None = None,
    ) -> tuple[float, float, float, float, float, float]:
        """What to add, for this process, to the account's, key's and project's day and month.

        Always: costs settled to nobody yet (plain increments, and what the
        counters refused); for every reservation still running or waiting for
        its settlement, the part of its recorded cost above its reservation;
        and the whole of a liability no longer held where it was recorded
        (unaccounted). With *reserved* -- for a check that read the counters
        only, not the reservations -- every reservation this process holds
        counts at the larger of its estimate and its recorded cost, a
        quarantined one included.

        A reservation running or waiting counts against today, as its row
        does. A liability counts against the day it ended on and that day's
        month, never against a later one: the database counts a quarantined
        row the same way, so this process's gate and every other's agree.

        *read_from* is for an admission, which adds this to what it read from
        the database: the moment (monotonic) from which its read was made.
        One of this process's settlements can commit after that read and be
        answered before the admission decides; the read then has the request
        at its estimate and the counters without its cost, and by the decision
        the process no longer holds it. So what settlements answered since
        *read_from* moved into the counters -- the part of a request's cost
        above its estimate, and the batch's plain costs -- is added too. A
        settlement that committed before the read and was answered after
        *read_from* is counted twice for that one admission, which errs
        towards refusing.
        """
        from serving.spend_ledger import utc_day

        today = utc_day()
        month = today[:7]
        totals = [0.0] * 6

        def add(line_user: str, line_key: int | None, line_project: str | None, day: str, cost):
            if not day.startswith(month):
                return
            value = float(cost)
            is_today = day == today
            if line_user == user_id:
                totals[1] += value
                if is_today:
                    totals[0] += value
            if key_id is not None and line_key == key_id:
                totals[3] += value
                if is_today:
                    totals[2] += value
            if project_id is not None and line_project == project_id:
                totals[5] += value
                if is_today:
                    totals[4] += value

        lines = [*self._increments, *self._set_aside]
        for batch in self._unsettled_batches():
            lines.extend(batch.increments)
        for line in lines:
            add(line.user_id, line.api_key_id, line.project_id, line.day, line.cost_usd)

        held_here = [*self._active.values(), *self._queued_reservations.values()]
        for reservation in held_here:
            known = reservation.cost()
            if reserved:
                held = max(reservation.amount, known)
            else:
                # The database holds its estimate; what it cost beyond that
                # is known only here.
                held = max(known - reservation.amount, Decimal(0))
            if held:
                add(reservation.user_id, reservation.key_id, reservation.project_id, today, held)
        for reservation in self._current_liabilities(month):
            if reservation.state == "quarantined" and not reserved:
                # Its row holds it, on the day it ended: the admission read it.
                continue
            # Its costs are in no counter, and the database holds nothing of it
            # (unaccounted) or holds it where a counters-only read does not
            # look (quarantined).
            add(
                reservation.user_id,
                reservation.key_id,
                reservation.project_id,
                reservation.ended_day or today,
                max(reservation.amount, reservation.cost()),
            )
        if read_from is not None:
            for answered_at, moved in self._landed:
                if answered_at >= read_from:
                    for line_user, line_key, line_project, day, cost in moved:
                        add(line_user, line_key, line_project, day or today, cost)
        return totals[0], totals[1], totals[2], totals[3], totals[4], totals[5]

    def _current_liabilities(self, month: str) -> list[SharedReservation]:
        """The liabilities that ended in *month* or later; earlier ones are set apart.

        One that ended in an earlier month counts against no day or month a
        check can still be made for, so it leaves the set every check reads and
        stays only in what this process reports (:meth:`holdings`).
        """
        past = [
            rid
            for rid, reservation in self._liabilities.items()
            if reservation.ended_day is not None and reservation.ended_day[:7] < month
        ]
        for rid in past:
            self._past_liabilities[rid] = self._liabilities.pop(rid)
        return list(self._liabilities.values())

    def _unsettled_batches(self) -> list[SettlementBatch]:
        """Every batch not yet answered, each once (the one being written is also queued)."""
        seen: dict[str, SettlementBatch] = {}
        candidates = [*self._replay, *self._solo]
        if self._writing is not None:
            candidates.append(self._writing)
        for batch in candidates:
            seen.setdefault(batch.batch_id, batch)
        return list(seen.values())

    # -- the gate ------------------------------------------------------------

    def refusal(self, check: SpendCheck) -> quota.BudgetRefusal | None:
        """The gate's early check: what it read plus what this process knows.

        Only this process's reservations are added: the authoritative check,
        with every process's, is the admission's.
        """
        from serving.spend_ledger import _refusal

        extra = self._local_extra(check.user_id, check.key_id, check.project_id, reserved=True)
        return _refusal(check, extra[:4], extra[4:])

    async def admit(
        self,
        check: SpendCheck,
        amount: float = quota.ESTIMATED_REQUEST_COST_USD,
        *,
        operation: str = "request",
    ) -> tuple[SharedReservation | None, quota.BudgetRefusal | None]:
        """Admit a request against *check*: reserve its cost in the database, or refuse it.

        The limits are the gate's; the spend is read again under the scopes'
        locks, every process's open reservations included. The policy is the
        per-process ledger's (:func:`serving.spend_ledger.decide_admission`).

        Raises:
            SpendAdmissionUnavailable: The database did not answer in
                :data:`ADMISSION_BUDGET_SECONDS` (no connection, a lock or a
                statement too slow, or a connection that failed).
            AccountErased: The account was hard-deleted meanwhile.
            ReservationConflict: The id was already used otherwise (a defect).
        """
        started = time.monotonic()
        outcome = "failed"
        try:
            reservation, refusal = await self._admit(check, amount, operation=operation)
            outcome = "admitted" if reservation is not None else "refused"
            return reservation, refusal
        except SpendAdmissionUnavailable as exc:
            outcome = "unavailable"
            self._admission_unavailable(exc)
            raise
        except AccountErased:
            outcome = "erased"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self._stats.admitted(outcome, time.monotonic() - started)
            self._start_upkeep()

    async def _admit(
        self, check: SpendCheck, amount: float, *, operation: str
    ) -> tuple[SharedReservation | None, quota.BudgetRefusal | None]:
        from serving.spend_ledger import decide_admission, utc_day

        request = AdmissionRequest(
            reservation_id=f"sr_{uuid.uuid4().hex}",
            instance_id=self._instance_id,
            user_id=check.user_id,
            api_key_id=check.key_id,
            project_id=check.project_id,
            requested_usd=usd(max(float(amount), 0.0)),
            check_day=utc_day(),
            operation=operation,
            check_key=check.key_id is not None
            and (
                check.key_budget_daily_usd is not None or check.key_budget_monthly_usd is not None
            ),
            check_project=check.project_id is not None
            and (
                check.project_budget_daily_usd is not None
                or check.project_budget_monthly_usd is not None
            ),
        )
        refused: list[quota.BudgetRefusal] = []
        xids: list[int] = []
        # From when each attempt's read is made: its locks are granted (the
        # store hands over the transaction id then) and the read follows.
        read_from = [time.monotonic()]

        def locked(xid: int) -> None:
            xids.append(xid)
            read_from[0] = time.monotonic()

        def decide(snapshot: SpendSnapshot) -> Decimal | None:
            local = self._local_extra(
                check.user_id,
                check.key_id,
                check.project_id,
                reserved=False,
                read_from=read_from[0],
            )
            read = dataclasses.replace(
                check,
                ledger=None,
                account_spent_usd=float(snapshot.account_day),
                account_spent_month_usd=float(snapshot.account_month),
                key_spent_today_usd=float(snapshot.key_day)
                if request.check_key
                else check.key_spent_today_usd,
                key_spent_month_usd=float(snapshot.key_month)
                if request.check_key
                else check.key_spent_month_usd,
                project_spent_today_usd=float(snapshot.project_day)
                if request.check_project
                else check.project_spent_today_usd,
                project_spent_month_usd=float(snapshot.project_month)
                if request.check_project
                else check.project_spent_month_usd,
            )
            extra = (
                float(snapshot.reserved_account_day) + local[0],
                float(snapshot.reserved_account_month) + local[1],
                float(snapshot.reserved_key_day) + local[2],
                float(snapshot.reserved_key_month) + local[3],
            )
            project_extra = (
                float(snapshot.reserved_project_day) + local[4],
                float(snapshot.reserved_project_month) + local[5],
            )
            decided, refusal = decide_admission(read, float(amount), extra, project_extra)
            if refusal is not None or decided is None:
                refused[:] = [refusal] if refusal is not None else []
                return None
            return usd(decided)

        from serving.storage.pools import request_connection

        ambiguous = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ADMISSION_BUDGET_SECONDS
        try:
            async with asyncio.timeout_at(deadline), self._local_locks(request.lock_keys()):
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        async with request_connection(
                            self._backend.request_pool, timeout=remaining
                        ) as conn:
                            outcome = await self._backend.admit_on(
                                conn,
                                request,
                                decide,
                                budget_ms=int((deadline - loop.time()) * 1000),
                                on_xid=locked,
                                pause=self.pause,
                            )
                        break
                    except DatabaseBusyError:
                        raise
                    except Exception as exc:
                        if xids:
                            ambiguous = True
                        if not _retryable(exc):
                            raise
                        # Same id: an attempt that did commit is found by it.
                        await asyncio.sleep(_ADMISSION_RETRY_PAUSE_SECONDS)
        except BaseException as exc:
            if xids:
                # Not dispatched; any attempt that did commit is released once
                # it is known to have ended.
                self._abandon(request, xids)
            if isinstance(exc, TimeoutError):
                raise SpendAdmissionUnavailable(
                    f"spend admission did not complete within {ADMISSION_BUDGET_SECONDS:g}s"
                ) from exc
            if isinstance(exc, Exception) and _database_error(exc):
                raise SpendAdmissionUnavailable(
                    f"spend admission failed: {type(exc).__name__}"
                ) from exc
            raise

        if outcome.result in ("admitted", "existing"):
            reservation = SharedReservation(
                self,
                request.reservation_id,
                user_id=request.user_id,
                key_id=request.api_key_id,
                project_id=request.project_id,
                amount=Decimal(outcome.reserved_usd or 0),
                check_day=request.check_day,
                operation=operation,
            )
            self._active[request.reservation_id] = reservation
            return reservation, None
        if ambiguous:
            self._abandon(request, xids)
        if outcome.result == "refused" and refused:
            return None, refused[0]
        if outcome.result == "erased":
            raise AccountErased(request.user_id)
        logger.error(
            "A spend reservation id was reused or already retired; not admitted",
            extra={
                "event": "spend_reservation_conflict",
                "reservation_id": request.reservation_id,
                "result": outcome.result,
            },
        )
        raise ReservationConflict(request.reservation_id)

    @contextlib.asynccontextmanager
    async def _local_locks(self, keys: list[tuple[int, int]]) -> AsyncIterator[None]:
        """Hold this process's lock of each scope, in order: one admission per scope at a time.

        Keeps a busy account's admissions from holding every request-pool
        connection while they wait for one another's database locks. Only the
        locks this admission acquired are released, whatever interrupts it;
        the use count of each key is kept apart from ownership.
        """
        registered: list[tuple[int, int]] = []
        acquired: list[asyncio.Lock] = []
        try:
            for key in keys:
                entry = self._locks.get(key)
                if entry is None:
                    entry = self._locks[key] = [asyncio.Lock(), 0]
                entry[1] += 1
                registered.append(key)
                lock = entry[0]
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
            for key in registered:
                entry = self._locks[key]
                entry[1] -= 1
                if entry[1] <= 0:
                    del self._locks[key]

    def _abandon(self, request: AdmissionRequest, xids: list[int]) -> None:
        self._abandoned.append(
            _Abandoned(
                request.reservation_id,
                request.user_id,
                request.api_key_id,
                request.project_id,
                list(xids),
            )
        )
        self._start_write()

    def _admission_unavailable(self, exc: Exception) -> None:
        """Ask for the alert that admissions are refused for the database's sake.

        Every such request is answered 503 and logged where it is answered
        (``DATABASE_BUSY``); this adds the page, asked for at most once a
        minute by this process and sent at most once per cooldown.
        """
        from serving.spend_ledger import _send_alert

        now = time.monotonic()
        last = self._unavailable_alert_at
        if last is not None and now - last < _UNAVAILABLE_ALERT_SECONDS:
            return
        self._unavailable_alert_at = now
        context = {
            "error": str(exc),
            "refused_since_start": self._stats.admissions.get("unavailable", 0) + 1,
            "logs": "error_code DATABASE_BUSY marks each request answered 503",
        }
        _send_alert(
            lambda alerts: alerts.alert_slack(
                alerts.AlertSeverity.ERROR,
                "Spend admission unavailable",
                context,
                dedupe_key="spend_admission_unavailable",
                cooldown_sec=3600,
            )
        )

    # -- recording ------------------------------------------------------------

    def record(
        self, user_id: str, cost_usd: float, *, api_key_id: int | None = None
    ) -> asyncio.Task[None] | None:
        """Record a request's cost: on its reservation while it runs, else as a plain increment.

        Synchronous, as :meth:`serving.spend_ledger.SpendLedger.record` is,
        with the same ceiling and rounding. A cost recorded with no reservation
        running for the same account, key and project -- no admission was
        made, the request had already ended (a late cost), or it is
        attributed elsewhere -- is added to the counters by the next batch,
        with no reservation to retire.
        """
        from serving.spend_ledger import capped_cost, utc_day

        if not user_id or not cost_usd > 0:
            return None
        cost_usd = capped_cost(user_id, cost_usd, api_key_id=api_key_id)
        context = req_ctx.get() or {}
        line = CostLine(
            user_id=user_id,
            day=utc_day(),
            api_key_id=api_key_id,
            cost_usd=usd(cost_usd),
            requests=1,
            project_id=context.get(req_ctx.PROJECT_ID) or None,
        )
        reservation = context.get(req_ctx.SPEND_RESERVATION)
        if (
            isinstance(reservation, SharedReservation)
            and reservation._ledger is self
            and reservation.owns(line.user_id, line.api_key_id, line.project_id)
            and reservation.charge(line)
        ):
            return self._writer
        self._increments.append(line)
        return self._start_write()

    def _complete(self, reservation: SharedReservation) -> None:
        from serving.spend_ledger import utc_day

        self._active.pop(reservation.reservation_id, None)
        reservation.state = "queued"
        reservation.ended_day = utc_day()
        reservation.ended_at = time.monotonic()
        self._queued[reservation.reservation_id] = reservation.member()
        self._queued_reservations[reservation.reservation_id] = reservation
        self._start_write()

    # -- writing ---------------------------------------------------------------

    def _has_work(self) -> bool:
        return bool(
            self._replay
            or self._solo
            or self._queued
            or self._increments
            or self._abandoned
            or self._marking
        )

    def _can_write(self) -> bool:
        """Whether a writer started now would have something to do.

        A mark waiting out its back-off is owed (:meth:`_has_work`) and is
        nothing to do yet: its own timer starts the writer for it.
        """
        return bool(
            self._replay
            or self._solo
            or self._queued
            or self._increments
            or self._abandoned
            or (self._marking and time.monotonic() >= self._mark_after)
        )

    def _start_write(self) -> asyncio.Task[None] | None:
        if self._writer is not None:
            return self._writer
        if self._closed or self._retry is not None or not self._can_write():
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = tracked_task(self._write_once(), name="spend_settlement")
        self._writer = task
        task.add_done_callback(self._write_finished)
        return task

    def _write_finished(self, task: asyncio.Task[None]) -> None:
        self._writer = None
        if task.cancelled():
            return
        # The next batch, unless a retry is waiting out its back-off (the
        # tracked task has swallowed and reported whatever the writer raised).
        self._start_write()
        self._start_upkeep()

    async def _resolve_abandoned(self) -> None:
        """Turn admissions known to have ended into release members; keep the rest waiting."""
        if not self._abandoned:
            return
        xids = sorted({x for entry in self._abandoned for x in entry.xids})
        try:
            status = await self._backend.transaction_status(xids)
        except Exception:
            logger.warning("Reading admission transaction status failed", exc_info=True)
            return
        waiting: list[_Abandoned] = []
        for entry in self._abandoned:
            states = [status.get(x) for x in entry.xids]
            if "in progress" in states:
                waiting.append(entry)
                continue
            if "committed" in states or None in states:
                # Its row may exist: retire it -- never dispatched, no charge.
                self._queued[entry.reservation_id] = Member(
                    reservation_id=entry.reservation_id,
                    user_id=entry.user_id,
                    api_key_id=entry.api_key_id,
                    project_id=entry.project_id,
                    kind="release",
                )
        self._abandoned = waiting

    def _next_batch(self) -> tuple[SettlementBatch | None, str]:
        if self._replay:
            return self._replay[0], "replay"
        if self._solo:
            return self._solo[0], "solo"
        if not self._queued and not self._increments:
            return None, "none"
        # Built from what is queued, which stays queued until the batch exists
        # and is kept for replay: a defect while building it (the writer then
        # backs off and tries again) finds everything still there. Taken out
        # first, a plain cost would be in no counter and counted nowhere, and
        # a reservation would stay held with nothing left to settle it.
        taken = list(self._queued)[:MAX_BATCH_MEMBERS]
        batch = SettlementBatch.make(
            self._instance_id,
            [self._queued[rid] for rid in taken],
            _coalesce(self._increments),
        )
        # Sent until it gets an answer, unchanged: a repeat of a batch that
        # did commit is recognised by its id.
        self._replay.append(batch)
        self._increments = []
        for rid in taken:
            del self._queued[rid]
        return batch, "new"

    async def _write_once(self) -> None:
        try:
            await self._write_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Not a failed write (those are answered in _write_batch, and the
            # batch sent again): a defect in this writer. It is tried again
            # after the usual back-off: not at once, where a defect that
            # persists would spin, and not never, which would leave what is
            # queued waiting for the next request to wake the writer.
            self._writing = None
            self._write_failed(exc)
            self._schedule_retry()
            raise

    async def _write_batch(self) -> None:
        await self._resolve_abandoned()
        await self._mark_refused()
        batch, source = self._next_batch()
        if batch is None:
            if self._abandoned:
                self._schedule_retry()
            return
        self._writing = batch
        try:
            result = await self._backend.settle(batch, pause=self.settle_pause)
        except asyncio.CancelledError:
            # Kept for replay (it may have committed); shutdown reports it.
            self._writing = None
            raise
        except Exception as exc:
            self._writing = None
            await self._failed(batch, source, exc)
            return
        self._writing = None
        self._done(batch, source, result)
        self._retry_delay = 0.0
        self._answered()

    async def _failed(self, batch: SettlementBatch, source: str, exc: Exception) -> None:
        from serving.spend_ledger import _refused, _unavailable

        entries = len(batch.members) + len(batch.increments)
        if _refused(exc):
            # The database answered: the batch did not commit, and sent again
            # it would fail again. Split it; a lone entry refused is kept out.
            self._answered()
            self._drop_from_queue(batch, source)
            if entries > 1:
                self._solo.extend(_split(batch))
                logger.warning(
                    "A settlement batch was refused; writing its entries one by one",
                    extra={
                        "event": "spend_settlement_split",
                        "entry_count": entries,
                        "sqlstate": getattr(exc, "sqlstate", None),
                    },
                )
                self._retry_delay = 0.0
                return
            await self._set_aside_refused(batch, exc)
            return
        logger.warning(
            "Writing a spend settlement failed; sending the same batch again",
            extra={
                "event": "spend_settlement_failed",
                "batch_id": batch.batch_id,
                "error_type": type(exc).__name__,
                "unavailable": _unavailable(exc),
                "entry_count": entries,
            },
        )
        self._write_failed(exc)
        self._schedule_retry()

    def _drop_from_queue(self, batch: SettlementBatch, source: str) -> None:
        queue = self._replay if source in ("replay", "new") else self._solo
        with contextlib.suppress(ValueError):
            queue.remove(batch)

    def _keep_liability(self, member: Member, state: str) -> SharedReservation | None:
        reservation = self._queued_reservations.pop(member.reservation_id, None)
        if reservation is None:
            return None
        reservation.state = state
        self._liabilities[member.reservation_id] = reservation
        return reservation

    async def _set_aside_refused(self, batch: SettlementBatch, exc: Exception) -> None:
        from serving.spend_ledger import _send_alert, utc_day

        sqlstate = getattr(exc, "sqlstate", None) or type(exc).__name__
        self._stats.entries_refused += len(batch.increments) + len(batch.members)
        self._set_aside.extend(batch.increments)
        for member in batch.members:
            if member.kind == "release":
                continue
            reservation = self._queued_reservations.get(member.reservation_id)
            ended = reservation.ended_day if reservation is not None else None
            # Marked below. Until that is answered its row is active at its
            # estimate, exactly as while it waited for this settlement, so it
            # stays here as one waiting: its cost above the estimate counted.
            self._marking.append((member, ended or utc_day(), f"settlement refused: {sqlstate}"))
        logger.error(
            "The counters refused a settlement entry; set aside and kept counted",
            extra={
                "event": "spend_settlement_refused",
                "batch_id": batch.batch_id,
                "sqlstate": sqlstate,
                "increments": [line.canonical() for line in batch.increments],
                "reservations": [m.reservation_id for m in batch.members],
            },
        )
        context = {
            "entries": len(batch.members) + len(batch.increments),
            "error": sqlstate,
            "logs": "event spend_settlement_refused lists each entry to reconcile",
        }
        _send_alert(
            lambda alerts: alerts.alert_slack(
                alerts.AlertSeverity.ERROR,
                "Spend settlement refused an entry",
                context,
                dedupe_key="spend_settlement_refused",
                cooldown_sec=3600,
            )
        )
        await self._mark_refused()

    async def _mark_refused(self) -> None:
        """Mark quarantined, in the database, the reservations whose costs the counters refused.

        What the mark's answer says decides what this process holds from then
        on, so nothing is assumed before it comes: a mark that fails is tried
        again (it is harmless to repeat), and the reservation stays a waiting
        one meanwhile.

        - ``quarantined``: its row now counts the larger of its estimate and
          its cost, on the day it ended. The part of the cost above the
          estimate, which only this process held, is kept a moment longer for
          an admission that read the row while it was still active
          (:meth:`_land`).
        - ``retired``: another batch retired it first (an operator's
          reconciliation of this process). Its costs go nowhere, and that is
          said (:meth:`_report_duplicates`).
        - ``erased``: the account was hard-deleted meanwhile. Dropped, as a
          settlement drops it.
        - ``missing``: no row and no receipt. Nothing in the database counts
          it, so this process does, in full (unaccounted).

        Marks have a back-off and a timer of their own (:meth:`_schedule_mark`).
        The writer's back-off holds every write back until it has run out; a
        mark that keeps failing must not do that to the settlements, which go
        on being written as they come. A mark that failed goes to the back of
        the list, so the ones behind it are tried as well. A pass stops after
        :data:`_MARK_FAILURES_PER_PASS` failures, and at the first one that
        is the database's own (no answer, no connection): the others would
        wait as long for the same, with the writer waiting on them.
        """
        from serving.spend_ledger import _send_alert, _unavailable

        if not self._marking:
            return
        if time.monotonic() < self._mark_after:
            self._schedule_mark()
            return
        failure: Exception | None = None
        failures = 0
        for _ in range(len(self._marking)):
            if failures >= _MARK_FAILURES_PER_PASS:
                break
            member, day, note = self._marking[0]
            try:
                outcome = await self._backend.quarantine(
                    member, self._instance_id, day=day, note=note
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._marking.append(self._marking.pop(0))
                failure = exc
                failures += 1
                if _unavailable(exc):
                    break
                continue
            self._marking.pop(0)
            if outcome == "quarantined":
                reservation = self._keep_liability(member, "quarantined")
                if reservation is not None:
                    over = reservation.cost() - reservation.amount
                    if over > 0:
                        self._land(
                            [
                                (
                                    reservation.user_id,
                                    reservation.key_id,
                                    reservation.project_id,
                                    None,
                                    over,
                                )
                            ]
                        )
            elif outcome in ("retired", "erased"):
                reservation = self._queued_reservations.pop(member.reservation_id, None)
                if reservation is not None:
                    reservation.state = "retired"
                if outcome == "retired":
                    self._report_duplicates([member], batch_id=None)
            else:
                self._keep_liability(member, "unaccounted")
                self._stats.members_unaccounted += 1
                logger.error(
                    "A refused reservation is no longer held as recorded; kept counted here",
                    extra={
                        "event": "spend_settlement_unaccounted",
                        "reservations": [(member.reservation_id, outcome)],
                        "amount_usd": str(member.cost_usd()),
                    },
                )
                context = {
                    "reservations": 1,
                    "logs": "event spend_settlement_unaccounted lists each reservation",
                }
                _send_alert(
                    lambda alerts, context=context: alerts.alert_slack(
                        alerts.AlertSeverity.ERROR,
                        "Spend settlement found reservations not held as recorded",
                        context,
                        dedupe_key="spend_settlement_unaccounted",
                        cooldown_sec=3600,
                    )
                )
        now = time.monotonic()
        if self._mark_retry is not None:
            self._mark_retry.cancel()
            self._mark_retry = None
        if failure is None:
            self._mark_after = self._mark_delay = 0.0
            self._mark_failing_since = None
            self._mark_failure_reported = False
            return
        self._mark_delay = min(_RETRY_MAX_SECONDS, max(_RETRY_FIRST_SECONDS, self._mark_delay * 2))
        self._mark_after = now + self._mark_delay
        self._schedule_mark()
        if self._mark_failing_since is None:
            self._mark_failing_since = now
        failing_for = now - self._mark_failing_since
        if failing_for >= SETTLEMENT_STALLED_SECONDS and not self._mark_failure_reported:
            # Once, and at ERROR: the refusal itself was alerted when it
            # happened, and until its mark lands the reservation stays held
            # as an active row, which only this process knows the cost of.
            self._mark_failure_reported = True
            logger.error(
                "Refused reservations cannot be marked quarantined; they stay held as active",
                extra={
                    "event": "spend_quarantine_mark_failing",
                    "failing_for_seconds": round(failing_for, 1),
                    "error_type": type(failure).__name__,
                    "reservations": [member.reservation_id for member, _, _ in self._marking],
                },
            )
        else:
            logger.warning(
                "Marking a refused reservation failed; it is tried again",
                exc_info=failure,
            )

    def _report_duplicates(self, dropped: list[Member], *, batch_id: str | None) -> None:
        """Say that reservations another batch retired first had costs, which were not added.

        An operator reconciled this process while it was running (their audit
        rows say what was charged instead), or a defect. Either way the charge
        recorded here is in no counter, and this line is the only place that
        says how much.
        """
        dropped = [member for member in dropped if member.costs]
        if not dropped:
            return
        self._stats.members_duplicate += len(dropped)
        logger.error(
            "Reservations were already retired by another batch; their costs were not added",
            extra={
                "event": "spend_settlement_duplicate",
                "batch_id": batch_id,
                "reservations": [m.reservation_id for m in dropped],
                "amount_usd": str(sum((m.cost_usd() for m in dropped), Decimal(0))),
            },
        )

    def _done(self, batch: SettlementBatch, source: str, result: SettlementResult) -> None:
        from serving.spend_ledger import _send_alert

        self._drop_from_queue(batch, source)
        if result.outcome in ("conflict", "invalid"):
            # Never expected: ids are random and payloads built here. Nothing
            # was written; everything stays counted here and is reported.
            self._stats.members_unaccounted += len(batch.members)
            self._set_aside.extend(batch.increments)
            for member in batch.members:
                self._keep_liability(member, "unaccounted")
            logger.error(
                "A settlement batch was refused before anything was written",
                extra={
                    "event": "spend_settlement_rejected",
                    "batch_id": batch.batch_id,
                    "outcome": result.outcome,
                    "detail": result.members.get("invalid"),
                },
            )
            return
        # What this batch moved out of this process's keeping and into the
        # counters: its plain costs, and the part of each request's cost above
        # the estimate its row held (see _local_extra's read_from).
        moved: list[tuple[Any, ...]] = [
            (line.user_id, line.api_key_id, line.project_id, line.day, line.cost_usd)
            for line in batch.increments
        ]
        unaccounted: list[Member] = []
        dropped: list[Member] = []
        for member in batch.members:
            what = result.outcome_of(member.reservation_id)
            if member.kind == "release":
                continue
            if what in ("settled", "released", "duplicate", "erased"):
                reservation = self._queued_reservations.pop(member.reservation_id, None)
                if reservation is not None:
                    reservation.state = "retired"
                    over = reservation.cost() - reservation.amount
                    if what == "settled" and over > 0:
                        moved.append(
                            (
                                reservation.user_id,
                                reservation.key_id,
                                reservation.project_id,
                                None,
                                over,
                            )
                        )
                if what == "duplicate":
                    dropped.append(member)
            else:
                # missing or mismatch: nothing holds it as recorded here.
                self._keep_liability(member, "unaccounted")
                unaccounted.append(member)
        self._land(moved)
        self._report_duplicates(dropped, batch_id=batch.batch_id)
        if unaccounted:
            self._stats.members_unaccounted += len(unaccounted)
            logger.error(
                "Settled reservations were not held as recorded; kept counted here",
                extra={
                    "event": "spend_settlement_unaccounted",
                    "batch_id": batch.batch_id,
                    "reservations": [
                        (m.reservation_id, result.outcome_of(m.reservation_id)) for m in unaccounted
                    ],
                    "amount_usd": str(sum((m.cost_usd() for m in unaccounted), Decimal(0))),
                },
            )
            context = {
                "reservations": len(unaccounted),
                "logs": "event spend_settlement_unaccounted lists each reservation",
            }
            _send_alert(
                lambda alerts: alerts.alert_slack(
                    alerts.AlertSeverity.ERROR,
                    "Spend settlement found reservations not held as recorded",
                    context,
                    dedupe_key="spend_settlement_unaccounted",
                    cooldown_sec=3600,
                )
            )

    def _land(self, moved: list[tuple[Any, ...]]) -> None:
        """Keep what an answered settlement moved into the counters, for a few seconds.

        For the admissions whose read may predate that settlement's commit
        (:meth:`_local_extra`); none can still be running after
        :data:`_LANDED_SECONDS`.
        """
        now = time.monotonic()
        while self._landed and self._landed[0][0] < now - _LANDED_SECONDS:
            self._landed.popleft()
        if moved:
            self._landed.append((now, tuple(moved)))

    def _schedule_retry(self) -> None:
        if self._retry is not None or self._closed:
            return
        self._retry_delay = min(
            _RETRY_MAX_SECONDS, max(_RETRY_FIRST_SECONDS, self._retry_delay * 2)
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._retry = loop.call_later(self._retry_delay, self._retry_now)

    def _retry_now(self) -> None:
        self._retry = None
        self._start_write()

    def _schedule_mark(self) -> None:
        """Wake the writer when the marks that failed may be tried again."""
        if self._mark_retry is not None or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._mark_retry = loop.call_later(
            max(0.0, self._mark_after - time.monotonic()), self._mark_now
        )

    def _mark_now(self) -> None:
        # Due by the timer's word, whatever the clocks make of the last instant.
        self._mark_retry = None
        self._mark_after = 0.0
        self._start_write()

    def _answered(self) -> None:
        """The database answered a batch: settlements are being written."""
        self._stats.batches_answered += 1
        self._failing_since = None
        self._stall_reported = False
        self._last_error = None

    def _write_failed(self, exc: BaseException) -> None:
        """A settlement write got no answer: count it, and report a stall that has lasted.

        While no write is answered, every request that ended keeps its whole
        estimate held in the database, so callers near a limit are refused for
        budget they have. Reported and alerted once per stall, when it has
        lasted :data:`SETTLEMENT_STALLED_SECONDS`.
        """
        from serving.spend_ledger import _send_alert

        now = time.monotonic()
        self._stats.write_failures += 1
        self._last_error = type(exc).__name__
        if self._failing_since is None:
            self._failing_since = now
        failing_for = now - self._failing_since
        if self._stall_reported or failing_for < SETTLEMENT_STALLED_SECONDS:
            return
        self._stall_reported = True
        waiting = _held(self._queued_reservations.values(), "ended_at", now)
        logger.error(
            "Spend settlements have not been written for a while; ended requests stay held",
            extra={
                "event": "spend_settlement_stalled",
                "failing_for_seconds": round(failing_for, 1),
                "error_type": self._last_error,
                "reservations": waiting["reservations"],
                "amount_usd": str(waiting["amount_usd"]),
            },
        )
        context = {
            "failing_for_seconds": round(failing_for),
            "error": self._last_error,
            "reservations_waiting": waiting["reservations"],
            "amount_held_usd": str(waiting["amount_usd"]),
            "logs": "event spend_settlement_failed has each attempt",
        }
        _send_alert(
            lambda alerts: alerts.alert_slack(
                alerts.AlertSeverity.ERROR,
                "Spend settlement is failing",
                context,
                dedupe_key="spend_settlement_stalled",
                cooldown_sec=3600,
            )
        )

    async def flush(self, timeout: float = 10.0) -> bool:
        """Send everything queued so far; True once nothing is left to send.

        True says nothing is waiting to be written. It is not a claim that
        everything is settled: :meth:`holdings` reports what still runs and
        what remains a liability.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # Marks waiting out a back-off are tried now, and from here on at the
        # shortest one.
        self._mark_after = 0.0
        while True:
            remaining = deadline - loop.time()
            if self._writer is not None:
                if remaining <= 0:
                    return False
                await asyncio.wait({self._writer}, timeout=remaining)
                continue
            if not self._has_work():
                return True
            if remaining <= 0:
                return False
            if self._retry is not None:
                self._retry.cancel()
                self._retry = None
                await asyncio.sleep(min(_RETRY_FIRST_SECONDS, remaining))
                continue
            if self._start_write() is None:
                await asyncio.sleep(min(_RETRY_FIRST_SECONDS, remaining))
                self._mark_after = 0.0

    async def close(self) -> bool:
        """Record that this process will send no settlement again; True once recorded.

        Shutdown's, after its :meth:`flush`. From here on this ledger sends
        nothing. The closure is recorded only with everything sent and
        answered and no request still running: it is what lets other processes
        delete this one's receipts once they are old enough, and a batch still
        unanswered would need its own. A process that leaves something unsent
        or a request running, or cannot reach the database, is not closed: its
        receipts, and whatever it still holds, stay until an operator says it
        is gone (``python -m serving.shared_spend reconcile``).
        """
        self._closed = True
        if self._retry is not None:
            self._retry.cancel()
            self._retry = None
        if self._mark_retry is not None:
            self._mark_retry.cancel()
            self._mark_retry = None
        if self._upkeep is not None:
            self._upkeep.cancel()
        if self._writer is not None or self._has_work() or self._active:
            return False
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
                await self._backend.close_instance(self._instance_id, closed_by="shutdown")
        except Exception:
            logger.warning("Recording this process's spend closure failed", exc_info=True)
            return False
        return True

    # -- upkeep -----------------------------------------------------------------

    def _start_upkeep(self) -> None:
        """Start the upkeep when it is due, unless it runs or this process is closing."""
        now = time.monotonic()
        if self._closed or self._upkeep is not None or now < self._upkeep_at:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._upkeep_at = now + UPKEEP_SECONDS
        task = tracked_task(self._keep_up(), name="spend_upkeep")
        self._upkeep = task
        task.add_done_callback(self._upkeep_finished)

    def _upkeep_finished(self, task: asyncio.Task[None]) -> None:
        self._upkeep = None

    async def _keep_up(self) -> None:
        """Delete the receipts nothing can need again; survey what every process holds.

        Best effort, off the request path and apart from the settlement
        writer: a failure is logged, and the next upkeep tries again.
        """
        try:
            # The batches this process may still send again keep their receipts;
            # one it makes from here on is younger than anything deleted.
            unanswered = [batch.batch_id for batch in self._unsettled_batches()]
            caught_up = False
            for _ in range(_UPKEEP_ROUNDS):
                batches, _ = await self._backend.purge_receipts(self._instance_id, unanswered)
                self._stats.receipts_purged += batches
                if batches < PURGE_BATCHES:
                    caught_up = True
                    break
            if not caught_up:
                self._upkeep_at = time.monotonic() + _UPKEEP_CATCH_UP_SECONDS
            await self._backend.purge_closures()
            # Estimated receipt counts: an exact count reads both tables whole,
            # every hour, from every process.
            found = survey(await self._backend.summary(exact=False))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "The shared spend upkeep failed; the next one tries again", exc_info=True
            )
            return
        self._survey = found
        self._surveyed_at = time.monotonic()
        self._report_stranded(found["stranded"])

    def _report_stranded(self, found: dict[str, Any]) -> None:
        """Report and alert reservations held by processes no longer heard from.

        Only once the oldest is :data:`STRANDED_ALERT_SECONDS` old. The
        registry cannot prove a process gone, so this asks an operator to
        look; it retires nothing.
        """
        from serving.spend_ledger import _send_alert

        oldest = found["oldest_age_seconds"]
        if not found["reservations"] or oldest is None or oldest < STRANDED_ALERT_SECONDS:
            return
        logger.error(
            "Reservations are held by processes no longer heard from; they stay counted",
            extra={
                "event": "spend_reservations_stranded",
                "reservations": found["reservations"],
                "amount_usd": str(found["amount_usd"]),
                "oldest_age_seconds": oldest,
                "instances": found["instances"],
            },
        )
        context = {
            "reservations": found["reservations"],
            "amount_held_usd": str(found["amount_usd"]),
            "oldest_hours": round(oldest / 3600, 1),
            "instances": ", ".join(found["instances"][:5]),
            "next": "python -m serving.shared_spend status, then reconcile INSTANCE_ID",
        }
        _send_alert(
            lambda alerts: alerts.alert_slack(
                alerts.AlertSeverity.ERROR,
                "Spend reservations held by a process no longer heard from",
                context,
                dedupe_key="spend_reservations_stranded",
                cooldown_sec=6 * 3600,
            )
        )

    # -- reporting --------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """What this process observed and holds, for ``/health/deep``; no account is named.

        Since the process started: admissions by outcome and how long they
        took, what it holds now (running requests, and ended ones waiting for
        their settlement, each with how long the oldest has), how its
        settlements went, and what its last hourly survey found in the
        database for every process (``database``, with its age).
        """
        now = time.monotonic()
        liabilities: dict[str, list[SharedReservation]] = {}
        for reservation in self._every_liability():
            liabilities.setdefault(reservation.state, []).append(reservation)
        failing = self._failing_since
        cost, records = self.unwritten()
        database = None
        if self._survey is not None and self._surveyed_at is not None:
            database = {"age_seconds": round(now - self._surveyed_at, 1), **self._survey}
        return {
            "instance_id": self._instance_id,
            "uptime_seconds": round(now - self._started, 1),
            "admissions": dict(self._stats.admissions),
            "admission_latency": self._stats.admission.snapshot(),
            "held": {
                "active": _held(self._active.values(), "admitted_at", now),
                "awaiting_settlement": _held(self._queued_reservations.values(), "ended_at", now),
                "quarantined": _held(liabilities.get("quarantined", ()), "ended_at", now),
                "unaccounted": _held(liabilities.get("unaccounted", ()), "ended_at", now),
            },
            "settlement": {
                "batches_answered": self._stats.batches_answered,
                "write_failures": self._stats.write_failures,
                "entries_refused": self._stats.entries_refused,
                "members_unaccounted": self._stats.members_unaccounted,
                "members_duplicate": self._stats.members_duplicate,
                "refused_awaiting_mark": len(self._marking),
                "failing_for_seconds": round(now - failing, 1) if failing is not None else None,
                "last_error": self._last_error,
                "unwritten_usd": round(cost, 6),
                "unwritten_records": records,
            },
            "receipts_purged": self._stats.receipts_purged,
            "database": database,
        }

    def unwritten(self) -> tuple[float, int]:
        """Costs recorded and not known added to the counters (dollars, cost records)."""
        lines = [*self._increments, *self._set_aside]
        for batch in self._unsettled_batches():
            lines.extend(batch.increments)
            lines.extend(c for m in batch.members for c in m.costs)
        queued_in_batches = {m.reservation_id for b in self._unsettled_batches() for m in b.members}
        for rid, member in self._queued.items():
            if rid not in queued_in_batches:
                lines.extend(member.costs)
        for member, _, _ in self._marking:
            lines.extend(member.costs)
        for reservation in self._every_liability():
            lines.extend(reservation.charges)
        return float(sum((line.cost_usd for line in lines), Decimal(0))), len(lines)

    def _every_liability(self) -> list[SharedReservation]:
        """Every liability this process holds, those of earlier months included."""
        return [*self._liabilities.values(), *self._past_liabilities.values()]

    def holdings(self) -> dict[str, int]:
        """What this process holds that is not settled, by kind, for status and shutdown."""
        in_batches = {m.reservation_id for b in self._unsettled_batches() for m in b.members}
        states: dict[str, int] = {}
        for reservation in self._every_liability():
            states[reservation.state] = states.get(reservation.state, 0) + 1
        marking = {member.reservation_id for member, _, _ in self._marking}
        return {
            "active": len(self._active),
            "queued": len(set(self._queued) | in_batches | marking),
            "abandoned": len(self._abandoned),
            "quarantined": states.get("quarantined", 0),
            "unaccounted": states.get("unaccounted", 0),
        }

    def fully_settled(self) -> bool:
        """Whether nothing this process admitted or recorded is unsettled or a liability."""
        return not any(self.holdings().values()) and self.unwritten()[1] == 0


def _held(reservations: Any, since: str, now: float) -> dict[str, Any]:
    """How many of *reservations* there are, what they hold, and how long the oldest has.

    Each holds the larger of its estimate and its recorded cost; *since* names
    the monotonic time it has been in this state from.
    """
    reservations = list(reservations)
    starts = [start for r in reservations if (start := getattr(r, since)) is not None]
    return {
        "reservations": len(reservations),
        "amount_usd": float(sum((max(r.amount, r.cost()) for r in reservations), Decimal(0))),
        "oldest_seconds": round(now - min(starts), 1) if starts else None,
    }


def _coalesce(lines: list[CostLine]) -> list[CostLine]:
    """Plain increments summed per account, key, day and project."""
    sums: dict[tuple[Any, ...], list[Any]] = {}
    for line in lines:
        key = (line.user_id, line.day, line.api_key_id, line.project_id)
        entry = sums.setdefault(key, [Decimal(0), 0])
        entry[0] += line.cost_usd
        entry[1] += line.requests
    return [
        CostLine(user, day, key_id, total, requests, project)
        for (user, day, key_id, project), (total, requests) in sums.items()
    ]


def _split(batch: SettlementBatch) -> list[SettlementBatch]:
    """One batch per entry of *batch*, each with a new id (the old one is known not committed)."""
    batches = [SettlementBatch.make(batch.instance_id, [], [line]) for line in batch.increments]
    batches += [SettlementBatch.make(batch.instance_id, [member], []) for member in batch.members]
    return batches


def _retryable(exc: Exception) -> bool:
    """Whether an admission attempt that failed so may be tried again with the same id."""
    from serving.spend_ledger import _unavailable

    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str):
        # The connection itself failing (class 08) is asyncpg's
        # ConnectionDoesNotExistError and ConnectionFailureError: what a lost
        # COMMIT answer looks like. Any other answer of the database's is
        # final, a cancelled statement (the budget ran out) included.
        return sqlstate in _RETRY_ADMISSION_SQLSTATES or sqlstate.startswith("08")
    if isinstance(exc, TimeoutError):
        return False
    return _unavailable(exc) or _connection_error(exc)


def _connection_error(exc: Exception) -> bool:
    try:
        import asyncpg
    except ImportError:  # pragma: no cover
        return False
    return isinstance(
        exc,
        asyncpg.exceptions.ConnectionDoesNotExistError
        | asyncpg.exceptions.InterfaceError
        | asyncpg.exceptions.ConnectionFailureError,
    )


def _database_error(exc: Exception) -> bool:
    """Whether *exc* is the database failing (answered 503), not a defect here."""
    from serving.spend_ledger import _unavailable

    if isinstance(getattr(exc, "sqlstate", None), str):
        return True
    return _unavailable(exc) or _connection_error(exc)


class SharedSpendUnsupported(RuntimeError):
    """The shared mode is on and the operational store cannot keep its accounting."""


# One ledger per root store, weakly, as the per-process ledgers are kept.
_LEDGERS: Any = None


def root_store(store: Any) -> Any:
    """The store the shared accounting is kept for, through any wrapper; None if unsupported.

    The protocol is ``shared_spend_root()``: the PostgreSQL store returns
    itself, and a wrapper (``CachedOperationalStore``) returns its wrapped
    store's root. Every caller that reaches the same database through the same
    root shares one ledger, so a request's admission and its costs meet.
    """
    root_of = getattr(store, "shared_spend_root", None)
    root = root_of() if callable(root_of) else None
    if root is None or not callable(getattr(root, "shared_spend_backend", None)):
        return None
    return root


def ledger_for(store: Any) -> SharedSpendLedger | None:
    """The shared ledger of operational store *store*, made on first use.

    Raises:
        SharedSpendUnsupported: *store* does not support the shared accounting
            (it must never fall back to the per-process ledger silently).
    """
    import weakref

    global _LEDGERS
    if store is None:
        return None
    root = root_store(store)
    if root is None:
        raise SharedSpendUnsupported(
            f"the shared spend accounting cannot be kept on a {type(store).__name__}"
        )
    if _LEDGERS is None:
        _LEDGERS = weakref.WeakKeyDictionary()
    ledger = _LEDGERS.get(root)
    if ledger is None:
        from serving.servers import restart

        ledger = _LEDGERS[root] = SharedSpendLedger(
            root.shared_spend_backend(), instance_id=restart.INSTANCE_ID
        )
    return ledger


def all_ledgers() -> list[SharedSpendLedger]:
    """Every shared ledger made so far, for shutdown's flush."""
    return list(_LEDGERS.values()) if _LEDGERS is not None else []
