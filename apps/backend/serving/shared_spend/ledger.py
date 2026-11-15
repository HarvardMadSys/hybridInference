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
database (``python -m serving.shared_spend status`` lists it), and one whose
costs the counters refused stays there quarantined.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from serving import quota
from serving.observability.tracked_tasks import tracked_task
from serving.shared_spend.store import (
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

#: SQLSTATEs an admission retries with the same reservation id: another
#: attempt's row just became visible (23505), a serialization failure (40).
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
    that outlives its settlement keeps counting against.
    """

    __slots__ = (
        "_ledger",
        "amount",
        "charges",
        "check_day",
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
        # Reservations past settlement that still hold a liability: quarantined
        # in the database (the counters refused their costs), or no longer held
        # as recorded (unaccounted, counted here in full). One counts against
        # the day it ended on and that day's month, as a quarantined row does
        # in the database; those of an earlier month count against nothing any
        # more and are set apart, for the report only. Never forgotten by this
        # process.
        self._liabilities: dict[str, SharedReservation] = {}
        self._past_liabilities: dict[str, SharedReservation] = {}
        self._writer: asyncio.Task[None] | None = None
        self._retry: asyncio.TimerHandle | None = None
        self._retry_delay = 0.0
        # Per scope key: the lock, and how many admissions use it now.
        self._locks: dict[tuple[int, int], list[Any]] = {}
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

        def decide(snapshot: SpendSnapshot) -> Decimal | None:
            local = self._local_extra(check.user_id, check.key_id, check.project_id, reserved=False)
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

        xids: list[int] = []
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
                                on_xid=xids.append,
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
            org_id=context.get(req_ctx.ORG_ID) or None,
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
        self._queued[reservation.reservation_id] = reservation.member()
        self._queued_reservations[reservation.reservation_id] = reservation
        self._start_write()

    # -- writing ---------------------------------------------------------------

    def _has_work(self) -> bool:
        return bool(
            self._replay or self._solo or self._queued or self._increments or self._abandoned
        )

    def _start_write(self) -> asyncio.Task[None] | None:
        if self._writer is not None:
            return self._writer
        if self._retry is not None or not self._has_work():
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
        increments = _coalesce(self._increments)
        self._increments = []
        members = [self._queued.pop(rid) for rid in list(self._queued)[:MAX_BATCH_MEMBERS]]
        return SettlementBatch.make(self._instance_id, members, increments), "new"

    async def _write_once(self) -> None:
        try:
            await self._write_batch()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Not a failed write (those are answered in _write_batch, and the
            # batch sent again): a defect in this writer. It is tried again
            # after the usual back-off: not at once, where a defect that
            # persists would spin, and not never, which would leave what is
            # queued waiting for the next request to wake the writer.
            self._writing = None
            self._schedule_retry()
            raise

    async def _write_batch(self) -> None:
        await self._resolve_abandoned()
        batch, source = self._next_batch()
        if batch is None:
            if self._abandoned:
                self._schedule_retry()
            return
        if source == "new":
            # Sent until it gets an answer, unchanged: a repeat of a batch
            # that did commit is recognised by its id.
            self._replay.append(batch)
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

    async def _failed(self, batch: SettlementBatch, source: str, exc: Exception) -> None:
        from serving.spend_ledger import _refused, _unavailable

        entries = len(batch.members) + len(batch.increments)
        if _refused(exc):
            # The database answered: the batch did not commit, and sent again
            # it would fail again. Split it; a lone entry refused is kept out.
            self._drop_from_queue(batch, source)
            if entries > 1:
                self._solo.extend(_split(batch))
                logger.warning(
                    "A settlement batch was refused; writing its entries one by one",
                    extra={
                        "event": "spend_settlement_split",
                        "entries": entries,
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
                "entries": entries,
            },
        )
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
        self._set_aside.extend(batch.increments)
        for member in batch.members:
            if member.kind == "release":
                continue
            reservation = self._keep_liability(member, "quarantined")
            ended = reservation.ended_day if reservation is not None else None
            try:
                await self._backend.quarantine(
                    member,
                    self._instance_id,
                    day=ended or utc_day(),
                    note=f"settlement refused: {sqlstate}",
                )
            except Exception:
                logger.warning("Marking a refused reservation failed", exc_info=True)
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

    def _done(self, batch: SettlementBatch, source: str, result: SettlementResult) -> None:
        from serving.spend_ledger import _send_alert

        self._drop_from_queue(batch, source)
        if result.outcome in ("conflict", "invalid"):
            # Never expected: ids are random and payloads built here. Nothing
            # was written; everything stays counted here and is reported.
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
        unaccounted: list[Member] = []
        for member in batch.members:
            what = result.outcome_of(member.reservation_id)
            if member.kind == "release":
                continue
            if what in ("settled", "released", "duplicate", "erased"):
                reservation = self._queued_reservations.pop(member.reservation_id, None)
                if reservation is not None:
                    reservation.state = "retired"
            else:
                # missing or mismatch: nothing holds it as recorded here.
                self._keep_liability(member, "unaccounted")
                unaccounted.append(member)
        if unaccounted:
            logger.error(
                "Settled reservations were not held as recorded; kept counted here",
                extra={
                    "event": "spend_settlement_unaccounted",
                    "batch_id": batch.batch_id,
                    "reservations": [
                        (m.reservation_id, result.outcome_of(m.reservation_id)) for m in unaccounted
                    ],
                    "cost_usd": str(sum((m.cost_usd() for m in unaccounted), Decimal(0))),
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

    def _schedule_retry(self) -> None:
        if self._retry is not None:
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

    async def flush(self, timeout: float = 10.0) -> bool:
        """Send everything queued so far; True once nothing is left to send.

        True says nothing is waiting to be written. It is not a claim that
        everything is settled: :meth:`holdings` reports what still runs and
        what remains a liability.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
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
        return {
            "active": len(self._active),
            "queued": len(set(self._queued) | in_batches),
            "abandoned": len(self._abandoned),
            "quarantined": states.get("quarantined", 0),
            "unaccounted": states.get("unaccounted", 0),
        }

    def fully_settled(self) -> bool:
        """Whether nothing this process admitted or recorded is unsettled or a liability."""
        return not any(self.holdings().values()) and self.unwritten()[1] == 0


def _coalesce(lines: list[CostLine]) -> list[CostLine]:
    """Plain increments summed per account, key, day, project and organization."""
    sums: dict[tuple[Any, ...], list[Any]] = {}
    for line in lines:
        key = (line.user_id, line.day, line.api_key_id, line.org_id, line.project_id)
        entry = sums.setdefault(key, [Decimal(0), 0])
        entry[0] += line.cost_usd
        entry[1] += line.requests
    return [
        CostLine(user, day, key_id, total, requests, org, project)
        for (user, day, key_id, org, project), (total, requests) in sums.items()
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
        return sqlstate in _RETRY_ADMISSION_SQLSTATES
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
