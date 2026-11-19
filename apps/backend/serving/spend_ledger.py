"""Spend the cost counters do not show yet: requests in flight, and costs not yet written.

The quota gates (``verify_api_key`` and the stored-key and grant doors) read
what an account and a key have spent from the counter tables,
``user_daily_cost`` and ``api_key_daily_cost``. Two kinds of spend are not in
those tables yet, and a gate that reads only the tables lets requests that run
at the same time overshoot a limit by as much as they cost together:

- **In flight.** An admitted request's cost is known only once it ends. Each
  request reserves an estimate when it is admitted (:meth:`SpendCheck.admit`),
  and gives it back when it ends -- by which time its real cost is recorded
  here, so there is no moment when it counts nowhere.
- **Recorded, not yet written.** A finished request's cost is recorded here
  (:meth:`SpendLedger.record`) and written to the counters by one writer per
  store. While one write is in flight the costs recorded meanwhile accumulate,
  one entry per user, key and UTC day, and go out together in the next: a
  burst of requests, or a slow database, costs one connection at a time and a
  bounded set of entries instead of one background task per request. A cost
  counts here until its write is confirmed, and for a while after that, for a
  gate whose read of the table may have been taken before the write landed.
  An entry the counters refuse for its own values (one that would overflow
  them) is set aside and reported rather than sent again, so it cannot hold
  back everyone else's.

A gate adds both to what it read (:meth:`SpendCheck.refusal`), and refuses a
request only when not even :data:`serving.quota.ESTIMATED_REQUEST_COST_USD`,
the optimistic charge every gate already adds for the request it is checking,
fits: a caller with nothing else running is answered exactly as before. What a
request then reserves is an estimate of its own cost
(:func:`serving.quota.estimate_request_cost`), up to what its limits have left
(:meth:`SpendLedger.admit`), so requests running together cannot overshoot a
limit by much more than one of them costs.

Everything here is per process. With several worker processes each one sees
only its own requests in flight and its own costs not yet written, so a limit
is protected within each process, not across them; production runs one. The
shared mode (:mod:`serving.shared_spend`, off unless an operator activates it)
keeps the same policy with its reservations in PostgreSQL, for every process
alike; :func:`ledger_for` hands out its ledger when the process runs in it.

Every amount here is in the deployment's accounting currency
(:mod:`serving.accounting`): the costs recorded are what the request was
charged (``CostTracker`` prices in that currency), the gate's limits and spends
are the native columns and counters, and the counters are written in it
(:attr:`~serving.storage.base.CostIncrement.cost_amount`). On a USD ledger that
is USD, exactly as before; the ``*_usd`` names here predate other ledgers.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from serving import quota
from serving.accounting import Money, get_accounting_policy
from serving.observability import detached_tasks
from serving.observability.tracked_tasks import tracked_task
from serving.storage.base import CostIncrement, CostWriteOutcomeUnknown
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

logger = get_logger(__name__)

#: Where an authenticated caller's context carries the :class:`SpendCheck` its
#: gate made, for the admission that reserves against it.
SPEND_CHECK_KEY = "spend_check"

#: How long a write stays counted here after it was confirmed: longer than any
#: gate takes from reading the counters to admitting the request, which the
#: request timeout bounds (120 s by default). A gate older than this counts
#: every confirmed write still kept, the conservative answer.
LANDED_WINDOW_SECONDS = 300.0

#: Retry delays after a failed write, doubling up to the last.
_RETRY_FIRST_SECONDS = 0.5
_RETRY_MAX_SECONDS = 30.0

#: The most one request adds to the counters. No real request comes near it:
#: $1,000 pays for two million tokens at $500 per million, past the largest
#: context window and the dearest price of any model served today. A larger
#: cost is a bad usage report or price -- a malformed or impostor upstream --
#: and taken whole it can overflow a counter, which holds under $1M
#: (``DECIMAL(12, 6)``).
MAX_REQUEST_COST_USD = 1000.0

#: SQLSTATE classes in which the counters refuse an entry's own values: 22, data
#: exception (a counter overflowing is 22003), and 23, integrity constraint
#: violation. Written again, the entry fails the same way.
_REFUSED_SQLSTATE_CLASSES = frozenset({"22", "23"})

#: SQLSTATE classes in which the database failed rather than an entry:
#: connection (08), transaction rollback (40), resources (53), operator
#: intervention (57) and system (58) errors.
_UNAVAILABLE_SQLSTATE_CLASSES = frozenset({"08", "40", "53", "57", "58"})

#: Entries written one by one that may fail in a row, none going in, before the
#: store is taken to be failing rather than those entries.
_MAX_FAILURES_BEFORE_ANY_WRITE = 3

_Key = tuple[str, int | None, str, str | None]  # user id, API key id, UTC day, project id


def utc_day(now: datetime | None = None) -> str:
    """The UTC day a cost recorded now is counted on, as the counters spell it."""
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


def counted_amount(cost: Money) -> Decimal:
    """What one increment of *cost* adds to a counter.

    PostgreSQL rounds each increment (half away from zero) to the counter's
    scale as it stores it. A batch written as one addition must add exactly
    what the separate increments would have, so each cost is rounded on its
    own before the batch sums them: to the ledger's quantum
    (:attr:`~serving.accounting.AccountingPolicy.quantum`), six places on a USD
    ledger's ``DECIMAL(12, 6)``, twelve on a native one's ``NUMERIC(24, 12)``.
    """
    return get_accounting_policy().quantize(cost)


def max_request_cost() -> Money:
    """:data:`MAX_REQUEST_COST_USD` in the ledger currency."""
    return get_accounting_policy().in_ledger(MAX_REQUEST_COST_USD)


def _logged_cost(counted: Decimal) -> dict[str, str]:
    """How a log record states *counted* (:meth:`~serving.accounting.AccountingPolicy.logged`)."""
    return get_accounting_policy().logged(counted)


@dataclass(slots=True)
class _Amount:
    """Costs recorded for one user, key and day: their sum, and how many."""

    cost: Money = field(default_factory=lambda: get_accounting_policy().zero)
    counted: Decimal = Decimal(0)
    requests: int = 0
    project_id: str | None = None

    def add(self, cost: Money, counted: Decimal, requests: int) -> None:
        self.cost += cost
        self.counted += counted
        self.requests += requests


@dataclass(slots=True)
class _Unconfirmed:
    """A user's costs not known to be in the counters, for one key and day."""

    # Nothing, in the ledger's arithmetic: a float on USD, a decimal natively,
    # so whatever is added later is of the same kind.
    cost: Money = field(default_factory=lambda: get_accounting_policy().zero)
    requests: int = 0
    project_id: str | None = None


@dataclass(slots=True)
class _Outcome:
    """What one write did with each entry of its batch, and the last error it met."""

    applied: list[_Key] = field(default_factory=list)
    # Not applied: written again later.
    failed: list[_Key] = field(default_factory=list)
    # Perhaps applied: never written again. Each with its error, which names
    # the transaction to reconcile.
    unknown: list[tuple[_Key, Exception]] = field(default_factory=list)
    # Refused for its own values: never written again either.
    refused: list[tuple[_Key, Exception]] = field(default_factory=list)
    error: Exception | None = None


class Reservation:
    """An admitted request's estimated cost, counted until :meth:`release`."""

    __slots__ = (
        "_ledger",
        "_open",
        "amount",
        "key_id",
        "organization_id",
        "project_id",
        "user_id",
    )

    def __init__(
        self,
        ledger: SpendLedger,
        user_id: str,
        key_id: int | None,
        amount: Money,
        project_id: str | None = None,
        organization_id: str | None = None,
    ) -> None:
        """Count *amount* against *user_id* (and *key_id*) until released."""
        self._ledger = ledger
        self.user_id = user_id
        self.key_id = key_id
        self.project_id = project_id
        self.organization_id = organization_id
        self.amount = amount
        self._open = True

    def release(self) -> None:
        """Stop counting the estimate. Idempotent."""
        if self._open:
            self._open = False
            self._ledger._unreserve(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class SpendCheck:
    """One gate's reading of an account's spend, and a key's, to admit a request on.

    ``read_at`` is :func:`time.monotonic` taken before the counters were read:
    any write confirmed earlier is in what was read, and any confirmed later
    may not be, so the ledger counts those again. ``key_id`` is the key the
    request's cost is attributed to; its budgets are the key's own limits,
    None where the key has none (or the request is not the key's).

    The limits and spends are in the ledger currency: on a non-USD ledger the
    gate builds the check from the native columns and counters, as exact
    decimals, and a refusal reports them with their USD equivalents
    (:func:`serving.quota.first_refusal_amount`).
    """

    #: The per-process :class:`SpendLedger`, the shared ledger, or None (no store).
    ledger: Any
    user_id: str
    key_id: int | None
    read_at: float
    account_quota_usd: Money
    account_spent_usd: Money
    account_quota_monthly_usd: Money | None = None
    account_spent_month_usd: Money = 0.0
    key_budget_daily_usd: Money | None = None
    key_budget_monthly_usd: Money | None = None
    key_spent_today_usd: Money = 0.0
    key_spent_month_usd: Money = 0.0
    key_prefix: str | None = None
    key_label: str | None = None

    project_id: str | None = None
    project_name: str | None = None
    project_budget_daily_usd: Money | None = None
    project_budget_monthly_usd: Money | None = None
    project_spent_today_usd: Money = 0.0
    project_spent_month_usd: Money = 0.0

    #: The organization the account is a member of, its monthly limit (None
    #: for no limit) and what its counter held for this UTC month when read.
    organization_id: str | None = None
    organization_name: str | None = None
    organization_limit_usd: Money | None = None
    organization_spent_month_usd: Money = 0.0

    def refusal(self) -> quota.BudgetRefusal | None:
        """The narrowest limit this request does not fit under, counting the ledger too."""
        if self.ledger is None:
            return _refusal(self, (0.0, 0.0, 0.0, 0.0))
        return self.ledger.refusal(self)

    def admit(
        self, amount: Money | None = None
    ) -> tuple[Reservation | None, quota.BudgetRefusal | None]:
        """Check again and reserve up to *amount*, in one step; see :meth:`SpendLedger.admit`.

        Without a ledger (no store) there is nothing to reserve in: only checked.
        The per-process ledger only; the shared one admits with :meth:`reserve`.
        *amount* defaults to the flat minimum (:func:`serving.quota.estimated_request_cost`).
        """
        if self.ledger is None:
            return None, self.refusal()
        if getattr(self.ledger, "shared", False):
            raise RuntimeError("a shared spend ledger admits asynchronously: use reserve()")
        return self.ledger.admit(self, amount)

    async def reserve(
        self, amount: Money | None = None, *, operation: str = "request"
    ) -> tuple[Any, quota.BudgetRefusal | None]:
        """Admit against this check in whichever mode the process runs.

        The per-process ledger admits as :meth:`admit` does, without suspending.
        The shared ledger (:meth:`serving.shared_spend.ledger.SharedSpendLedger.admit`)
        reserves in the database, and raises a
        :class:`~serving.storage.pools.DatabaseBusyError` when it cannot in time.
        *operation* names what is admitted (``request``, ``batch``,
        ``playground``), recorded with a shared reservation.
        """
        if amount is None:
            amount = quota.estimated_request_cost()
        if self.ledger is not None and getattr(self.ledger, "shared", False):
            return await self.ledger.admit(self, amount, operation=operation)
        return self.admit(amount)


def decide_admission(
    check: SpendCheck,
    amount: Money | None,
    extra: tuple[Money, Money, Money, Money],
    project_extra: tuple[Money, Money] = (0.0, 0.0),
    organization_extra: Money = 0.0,
) -> tuple[Money | None, quota.BudgetRefusal | None]:
    """The admission policy: refuse, or what to reserve, given *extra* on top of *check*.

    Refused only when not even the flat minimum
    (:func:`~serving.quota.estimated_request_cost`) fits; otherwise *amount*
    (the minimum when None), but no more than the tightest limit has left and
    never less than that minimum. Both ledgers apply exactly this, in the
    ledger's arithmetic (:meth:`~serving.accounting.AccountingPolicy.number`).
    """
    refusal = _refusal(check, extra, project_extra, organization_extra)
    if refusal is not None:
        return None, refusal
    number = get_accounting_policy().number
    minimum = quota.estimated_request_cost()
    if amount is None or number(amount) <= minimum:
        return minimum, None
    headroom = _headroom(check, extra, project_extra, organization_extra)
    return max(minimum, min(number(amount), headroom)), None


def _refusal(
    check: SpendCheck,
    extra: tuple[Money, Money, Money, Money],
    project_extra: tuple[Money, Money] = (0.0, 0.0),
    organization_extra: Money = 0.0,
) -> quota.BudgetRefusal | None:
    """Apply *check*'s limits to what it read plus *extra* (day, month, key day, key month).

    *project_extra* is the project's day and month on top of what was read;
    *organization_extra* the organization's month.
    """
    number = get_accounting_policy().number
    day, month, key_day, key_month = (number(value) for value in extra)
    project_day, project_month = (number(value) for value in project_extra)

    def spent(value: Money) -> Money:
        return number(value)

    return quota.first_refusal(
        account_quota_usd=check.account_quota_usd,
        account_spent_usd=spent(check.account_spent_usd) + day,
        account_quota_monthly_usd=check.account_quota_monthly_usd,
        account_spent_month_usd=spent(check.account_spent_month_usd) + month,
        key_budget_daily_usd=check.key_budget_daily_usd,
        key_budget_monthly_usd=check.key_budget_monthly_usd,
        key_spent_today_usd=spent(check.key_spent_today_usd) + key_day,
        key_spent_month_usd=spent(check.key_spent_month_usd) + key_month,
        key_prefix=check.key_prefix,
        key_label=check.key_label,
        project_budget_daily_usd=check.project_budget_daily_usd,
        project_budget_monthly_usd=check.project_budget_monthly_usd,
        project_spent_today_usd=spent(check.project_spent_today_usd) + project_day,
        project_spent_month_usd=spent(check.project_spent_month_usd) + project_month,
        project_name=check.project_name,
        organization_limit_usd=check.organization_limit_usd,
        organization_spent_month_usd=(
            spent(check.organization_spent_month_usd) + number(organization_extra)
        ),
        organization_name=check.organization_name,
    )


def _headroom(
    check: SpendCheck,
    extra: tuple[Money, Money, Money, Money],
    project_extra: tuple[Money, Money] = (0.0, 0.0),
    organization_extra: Money = 0.0,
) -> Money:
    """What the tightest of *check*'s limits has left, counting *extra* (see :func:`_refusal`)."""
    number = get_accounting_policy().number
    day, month, key_day, key_month = (number(value) for value in extra)
    project_day, project_month = (number(value) for value in project_extra)
    left = [number(check.account_quota_usd) - (number(check.account_spent_usd) + day)]
    if check.account_quota_monthly_usd is not None:
        left.append(
            number(check.account_quota_monthly_usd)
            - (number(check.account_spent_month_usd) + month)
        )
    if check.key_budget_daily_usd is not None:
        left.append(
            number(check.key_budget_daily_usd) - (number(check.key_spent_today_usd) + key_day)
        )
    if check.key_budget_monthly_usd is not None:
        left.append(
            number(check.key_budget_monthly_usd) - (number(check.key_spent_month_usd) + key_month)
        )
    if check.project_budget_daily_usd is not None:
        left.append(
            number(check.project_budget_daily_usd)
            - number(check.project_spent_today_usd)
            - project_day
        )
    if check.project_budget_monthly_usd is not None:
        left.append(
            number(check.project_budget_monthly_usd)
            - number(check.project_spent_month_usd)
            - project_month
        )
    if check.organization_limit_usd is not None:
        left.append(
            number(check.organization_limit_usd)
            - number(check.organization_spent_month_usd)
            - number(organization_extra)
        )
    return min(left)


def spend_check_of(user_ctx: Mapping[str, Any] | None) -> SpendCheck | None:
    """The :class:`SpendCheck` an authenticated caller's gate made, if it made one."""
    check = user_ctx.get(SPEND_CHECK_KEY) if user_ctx else None
    return check if isinstance(check, SpendCheck) else None


class SpendLedger:
    """In-flight reservations and unwritten costs for one operational store."""

    def __init__(self, store: Any) -> None:
        """Keep the ledger of *store*; see :func:`ledger_for`."""
        try:
            self._store: Callable[[], Any] = weakref.ref(store)
        except TypeError:
            self._store = lambda: store
        self._reserved_projects: dict[str, list[Any]] = {}
        self._reserved_users: dict[str, list[Any]] = {}
        self._reserved_keys: dict[int, list[Any]] = {}
        self._reserved_organizations: dict[str, list[Any]] = {}
        # The members of each organization a gate here has checked a request
        # of. An organization's limit counts the costs all of them have not
        # yet written; a member this process never served has none.
        self._organization_users: dict[str, set[str]] = {}
        # Recorded, not yet handed to a write (or handed back by a failed one).
        self._pending: dict[_Key, _Amount] = {}
        # Handed to the write in flight.
        self._writing: dict[_Key, _Amount] = {}
        # Per user, everything recorded and not yet confirmed written: pending,
        # being written, written with an outcome nobody could tell, or refused.
        self._unconfirmed: dict[str, dict[tuple[int | None, str, str | None], _Unconfirmed]] = {}
        # Writes confirmed: (when, {user: [(key, day, cost), ...]}).
        self._landed: deque[
            tuple[float, dict[str, list[tuple[int | None, str, Money, str | None]]]]
        ] = deque()
        self._unknown: list[tuple[_Key, _Amount]] = []
        # Entries the counters refused for their own values, set aside.
        self._refused: list[tuple[_Key, _Amount]] = []
        self._writer: asyncio.Task[None] | None = None
        self._retry: asyncio.TimerHandle | None = None
        self._retry_delay = 0.0

    # -- the gate -----------------------------------------------------------

    def refusal(self, check: SpendCheck) -> quota.BudgetRefusal | None:
        """Apply the gate's limits to what it read plus what only this ledger knows."""
        project_extra = (0.0, 0.0)
        if check.project_id is not None:
            values = self._extra(
                check.user_id, check.key_id, check.read_at, project_id=check.project_id
            )
            project_extra = values[:2]
        return _refusal(
            check,
            self._extra(check.user_id, check.key_id, check.read_at),
            project_extra,
            self._organization_extra(check),
        )

    def admit(
        self, check: SpendCheck, amount: Money | None = None
    ) -> tuple[Reservation | None, quota.BudgetRefusal | None]:
        """Admit a request against *check*: reserve its cost, or say which limit refuses it.

        The check and the reservation happen in one step with nothing awaited
        between them, so two requests admitted at the same moment cannot both
        fit into the same remaining budget. Whoever takes the reservation
        releases it when the request ends.

        The request is refused only when not even the flat minimum
        (:func:`~serving.quota.estimated_request_cost`) fits, as every gate
        refuses. It then reserves *amount*, what it may cost -- but no more than
        its tightest limit has left, and never less than that minimum: a
        request is not refused because its worst case does not fit, yet while
        it runs it holds what is left, so the requests admitted alongside it
        cannot overshoot the limit together.
        """
        extra = self._extra(check.user_id, check.key_id, check.read_at)
        project_extra = (
            self._extra(check.user_id, check.key_id, check.read_at, project_id=check.project_id)[:2]
            if check.project_id
            else (0.0, 0.0)
        )
        decided, refusal = decide_admission(
            check, amount, extra, project_extra, self._organization_extra(check)
        )
        if refusal is not None or decided is None:
            return None, refusal
        amount = decided
        reservation = Reservation(
            self, check.user_id, check.key_id, amount, check.project_id, check.organization_id
        )
        if check.project_id is not None:
            _add_reserved(self._reserved_projects, check.project_id, amount)
        if check.organization_id is not None:
            _add_reserved(self._reserved_organizations, check.organization_id, amount)
        _add_reserved(self._reserved_users, check.user_id, amount)
        if check.key_id is not None:
            _add_reserved(self._reserved_keys, check.key_id, amount)
        return reservation, None

    def _unreserve(self, reservation: Reservation) -> None:
        if reservation.project_id is not None:
            _remove_reserved(self._reserved_projects, reservation.project_id, reservation.amount)
        if reservation.organization_id is not None:
            _remove_reserved(
                self._reserved_organizations, reservation.organization_id, reservation.amount
            )
        _remove_reserved(self._reserved_users, reservation.user_id, reservation.amount)
        if reservation.key_id is not None:
            _remove_reserved(self._reserved_keys, reservation.key_id, reservation.amount)

    def _extra(
        self, user_id: str, key_id: int | None, read_at: float, *, project_id: str | None = None
    ) -> tuple[Money, Money, Money, Money]:
        """What to add to the account's day and month, and the key's, for this gate."""
        today = utc_day()
        month = today[:7]
        zero = get_accounting_policy().zero
        totals = [zero, zero, zero, zero]

        def count(cost_key: int | None, day: str, cost: Money, cost_project: str | None) -> None:
            if project_id is not None and cost_project != project_id:
                return
            if day.startswith(month):
                totals[1] += cost
                if day == today:
                    totals[0] += cost
                if key_id is not None and cost_key == key_id:
                    totals[3] += cost
                    if day == today:
                        totals[2] += cost

        for (cost_key, day, _project), amount in self._unconfirmed.get(user_id, {}).items():
            count(cost_key, day, amount.cost, amount.project_id)
        now = time.monotonic()
        self._forget_landed(now)
        stale = read_at < now - LANDED_WINDOW_SECONDS
        for confirmed_at, by_user in reversed(self._landed):
            if confirmed_at < read_at and not stale:
                break
            for cost_key, day, cost, cost_project in by_user.get(user_id, ()):
                count(cost_key, day, cost, cost_project)

        reserved = (
            self._reserved_projects.get(project_id)
            if project_id is not None
            else self._reserved_users.get(user_id)
        )
        if reserved is not None:
            totals[0] += reserved[0]
            totals[1] += reserved[0]
        if key_id is not None and (reserved := self._reserved_keys.get(key_id)) is not None:
            totals[2] += reserved[0]
            totals[3] += reserved[0]
        return totals[0], totals[1], totals[2], totals[3]

    def _organization_extra(self, check: SpendCheck) -> Money:
        """What to add to the month of *check*'s organization, for this gate.

        Everything the organization's members have in this process that the
        organization's counter did not show the gate: their costs not yet
        confirmed written, those confirmed since the counter was read, and
        what their requests in flight reserve. The reservations are a running
        total, as a project's are. The costs are kept by account, so they are
        summed for the members a gate here has seen: one pass over what is
        unwritten and what has just landed, however many members there are.
        """
        zero = get_accounting_policy().zero
        organization_id = check.organization_id
        if organization_id is None:
            return zero
        members = self._organization_users.setdefault(organization_id, set())
        members.add(check.user_id)
        if check.organization_limit_usd is None:
            return zero
        month = utc_day()[:7]
        total = zero
        for user_id, entries in self._unconfirmed.items():
            if user_id in members:
                for (_key, day, _project), amount in entries.items():
                    if day.startswith(month):
                        total += amount.cost
        now = time.monotonic()
        self._forget_landed(now)
        stale = check.read_at < now - LANDED_WINDOW_SECONDS
        for confirmed_at, by_user in reversed(self._landed):
            if confirmed_at < check.read_at and not stale:
                break
            for user_id, costs in by_user.items():
                if user_id in members:
                    for _key, day, cost, _project in costs:
                        if day.startswith(month):
                            total += cost
        reserved = self._reserved_organizations.get(organization_id)
        if reserved is not None:
            total += reserved[0]
        return total

    def _forget_landed(self, now: float) -> None:
        while self._landed and self._landed[0][0] < now - LANDED_WINDOW_SECONDS:
            self._landed.popleft()

    # -- recording and writing ----------------------------------------------

    def record(
        self, user_id: str, cost_usd: Money, *, api_key_id: int | None = None
    ) -> asyncio.Task[None] | None:
        """Record a finished request's cost, to be added to the counters.

        Synchronous: it runs where a request is torn down, which must not
        suspend. The write follows at once when none is in flight, else with
        the next. Returns the write task that will carry it, or the one in
        flight, or None when no write could be started (no running loop, or
        waiting out a failed one); everything recorded is written either way.

        A cost above :data:`MAX_REQUEST_COST_USD` cannot be a real request's: it
        is recorded as that ceiling, and logged and alerted for an operator.
        *cost_usd* is in the ledger currency, as is the ceiling it is held to
        (:func:`max_request_cost`).
        """
        if not user_id or not cost_usd > 0:
            return None
        policy = get_accounting_policy()
        cost_usd = policy.number(capped_cost(user_id, cost_usd, api_key_id=api_key_id))
        day = utc_day()
        context = req_ctx.get() or {}
        project_id = context.get(req_ctx.PROJECT_ID) or None
        key: _Key = (user_id, api_key_id, day, project_id)
        self._pending.setdefault(key, _Amount(cost=policy.zero, project_id=project_id)).add(
            cost_usd, counted_amount(cost_usd), 1
        )
        unconfirmed = self._unconfirmed.setdefault(user_id, {}).setdefault(
            (api_key_id, day, project_id), _Unconfirmed(cost=policy.zero, project_id=project_id)
        )
        unconfirmed.cost += cost_usd
        unconfirmed.requests += 1
        return self._start_write()

    def _start_write(self) -> asyncio.Task[None] | None:
        if self._writer is not None:
            return self._writer
        if self._retry is not None or not self._pending:
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = tracked_task(self._write_pending(), name="cost_increment")
        self._writer = task
        task.add_done_callback(self._write_finished)
        return task

    def _write_finished(self, _task: asyncio.Task[None]) -> None:
        self._writer = None
        self._start_write()

    async def _write_pending(self) -> None:
        batch, self._pending = self._pending, {}
        if not batch:
            return
        store = self._store()
        if store is None:
            # The store is gone (only a test drops one): nothing to write to.
            for key, amount in batch.items():
                self._unconfirm(key, amount)
            return
        self._writing = batch
        try:
            outcome = await _write(store, batch)
        except BaseException:
            # Cancelled mid-write (shutdown): whether it landed is unknown.
            self._mark_unknown(list(batch.items()), None)
            raise
        finally:
            self._writing = {}
        if outcome.applied:
            self._land(outcome.applied, batch)
        for error, keys in _by_error(outcome.unknown):
            self._mark_unknown([(key, batch[key]) for key in keys], error)
        if outcome.refused:
            self._set_aside([(key, batch[key], error) for key, error in outcome.refused])
        if outcome.failed:
            for key in outcome.failed:
                self._pending.setdefault(key, _Amount(project_id=batch[key].project_id)).add(
                    batch[key].cost, batch[key].counted, batch[key].requests
                )
            self._schedule_retry()
        else:
            self._retry_delay = 0.0
        if outcome.error is not None:
            logger.warning(
                "Writing the cost counters failed; %s",
                "retrying" if outcome.failed else "not retried",
                extra={
                    "event": "cost_counter_write_failed",
                    "error_type": type(outcome.error).__name__,
                    "entries": len(batch),
                    "applied": len(outcome.applied),
                    "failed": len(outcome.failed),
                    "unknown": len(outcome.unknown),
                    "refused": len(outcome.refused),
                },
            )
            raise outcome.error

    def _land(self, keys: list[_Key], batch: dict[_Key, _Amount]) -> None:
        by_user: dict[str, list[tuple[int | None, str, float, str | None]]] = {}
        for key in keys:
            amount = batch[key]
            self._unconfirm(key, amount)
            by_user.setdefault(key[0], []).append((key[1], key[2], amount.cost, amount.project_id))
        now = time.monotonic()
        self._landed.append((now, by_user))
        self._forget_landed(now)

    def _unconfirm(self, key: _Key, amount: _Amount) -> None:
        user_id, key_id, day, project_id = key
        per_user = self._unconfirmed.get(user_id)
        entry = per_user.get((key_id, day, project_id)) if per_user else None
        if per_user is None or entry is None:
            return
        entry.requests -= amount.requests
        entry.cost -= amount.cost
        if entry.requests <= 0:
            del per_user[(key_id, day, project_id)]
            if not per_user:
                del self._unconfirmed[user_id]

    def _mark_unknown(
        self, entries: list[tuple[_Key, _Amount]], error: BaseException | None
    ) -> None:
        # Not written again (it may have landed) and still counted here (it
        # may not have): the conservative side of both. Logged with what a
        # reconciliation needs.
        self._unknown.extend(entries)
        logger.error(
            "Cost counter write outcome unknown; not retried, still counted in this process",
            extra={
                "event": "cost_counter_write_unknown",
                "xid": getattr(error, "xid", None),
                "entries": [
                    {
                        "user_id": key[0],
                        "api_key_id": key[1],
                        "day": key[2],
                        **_logged_cost(amount.counted),
                        "requests": amount.requests,
                    }
                    for key, amount in entries
                ],
            },
        )

    def _set_aside(self, entries: list[tuple[_Key, _Amount, Exception]]) -> None:
        # Written alone, each was refused for its own values (a counter would
        # overflow, say), so writing it again would fail again -- and, written
        # with others, hold their costs back too. Kept out of the counters and
        # still counted here, like an unknown outcome, for an operator to
        # reconcile from this record.
        self._refused.extend((key, amount) for key, amount, _ in entries)
        logger.error(
            "The cost counters refused entries; set aside, still counted in this process",
            extra={
                "event": "cost_counter_write_refused",
                "entries": [
                    {
                        "user_id": key[0],
                        "api_key_id": key[1],
                        "day": key[2],
                        **_logged_cost(amount.counted),
                        "requests": amount.requests,
                        "error_type": type(error).__name__,
                        "sqlstate": getattr(error, "sqlstate", None),
                    }
                    for key, amount, error in entries
                ],
            },
        )
        context = {
            "entries": len(entries),
            **_logged_cost(sum((amount.counted for _, amount, _ in entries), Decimal(0))),
            "error": type(entries[0][2]).__name__,
            "logs": "event cost_counter_write_refused lists each entry to reconcile",
        }
        _send_alert(
            lambda alerts: alerts.alert_slack(
                alerts.AlertSeverity.ERROR,
                "Cost counters refused an entry",
                context,
                dedupe_key="cost_counter_write_refused",
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
        """Write everything recorded so far; True once nothing is left unwritten.

        Waits for a write in flight, retries a failed one at once rather than
        after its back-off, and gives up after *timeout* seconds.
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
            if not self._pending:
                return True
            if remaining <= 0:
                return False
            if self._retry is not None:
                self._retry.cancel()
                self._retry = None
                await asyncio.sleep(min(_RETRY_FIRST_SECONDS, remaining))
                continue
            self._start_write()

    def handoff_report(self) -> dict[str, Any]:
        """What this ledger still has to write, and what it never will, for a release handoff.

        ``owed`` is recorded and not yet written, or being written: it is
        written within seconds while the counters take writes (``finished``
        when none is left). A write whose outcome is unknown is never retried
        and a refused entry never written again: neither is confirmed in the
        counters, and neither resolves by waiting. They make the ledger
        unconfirmed (``confirmed`` false) for as long as this process lives,
        which is what keeps it from being sealed and stopped as if its books
        were complete. Only costs that were recorded count: a request that
        cost nothing was never recorded, and stays out of all of this.
        """
        owed = [*self._pending.values(), *self._writing.values()]
        unconfirmed = [amount for _, amount in [*self._unknown, *self._refused]]
        policy = get_accounting_policy()
        owed_cost = sum(a.cost for a in owed)
        unconfirmed_cost = sum(a.cost for a in unconfirmed)
        native = (
            {
                "owed_amount": str(sum((a.counted for a in owed), Decimal(0))),
                "unconfirmed_amount": str(sum((a.counted for a in unconfirmed), Decimal(0))),
                "currency": policy.currency,
            }
            if policy.native
            else {}
        )
        return {
            "mode": "local",
            "owed_usd": round(float(policy.as_usd(owed_cost)), 6),
            "owed_requests": sum(a.requests for a in owed),
            "writing": self._writer is not None,
            "unknown_requests": sum(a.requests for _, a in self._unknown),
            "refused_requests": sum(a.requests for _, a in self._refused),
            "unconfirmed_usd": round(float(policy.as_usd(unconfirmed_cost)), 6),
            "unconfirmed_requests": sum(a.requests for a in unconfirmed),
            "finished": not owed and self._writer is None,
            "confirmed": not unconfirmed,
            **native,
        }

    def unwritten(self) -> tuple[Money, int]:
        """Costs recorded and not known written (ledger amount, requests), for shutdown's report.

        What is pending, what a write still in flight carries, what was
        written with an outcome nobody could tell, and what the counters
        refused.
        """
        amounts = [*self._pending.values(), *self._writing.values()]
        amounts += [amount for _, amount in self._unknown]
        amounts += [amount for _, amount in self._refused]
        return sum(a.cost for a in amounts), sum(a.requests for a in amounts)


def capped_cost(user_id: str, cost_usd: Money, *, api_key_id: int | None = None) -> Money:
    """*cost_usd*, or :data:`max_request_cost()` when beyond it -- logged and alerted.

    Both are ledger amounts (the name predates other ledgers).
    """
    ceiling = max_request_cost()
    if cost_usd <= ceiling:
        return cost_usd
    stated = _ceiling_fields(cost_usd, ceiling)
    logger.error(
        "A request's cost is beyond any real request's; recording the ceiling",
        extra={
            "event": "cost_above_request_ceiling",
            "user_id": user_id,
            "api_key_id": api_key_id,
            **stated,
        },
    )
    context = {
        **stated,
        "logs": "event cost_above_request_ceiling names the account",
    }
    _send_alert(
        lambda alerts: alerts.alert_slack(
            alerts.AlertSeverity.ERROR,
            "Request cost above the per-request ceiling",
            context,
            dedupe_key="cost_above_request_ceiling",
            cooldown_sec=3600,
        )
    )
    return ceiling


def _ceiling_fields(cost: Money, ceiling: Money) -> dict[str, Any]:
    """How the ceiling alert states a cost: in dollars on USD, natively on another ledger."""
    policy = get_accounting_policy()
    if not policy.native:
        return {"cost_usd": repr(cost), "ceiling_usd": ceiling}
    return {"cost_amount": str(cost), "ceiling_amount": str(ceiling), "currency": policy.currency}


def _add_reserved(table: dict[Any, list[Any]], who: Any, amount: Money) -> None:
    entry = table.get(who)
    if entry is None:
        table[who] = [amount, 1]
    else:
        entry[0] += amount
        entry[1] += 1


def _remove_reserved(table: dict[Any, list[Any]], who: Any, amount: Money) -> None:
    entry = table.get(who)
    if entry is None:
        return
    entry[1] -= 1
    if entry[1] <= 0:
        del table[who]
    else:
        entry[0] -= amount


async def _write(store: Any, batch: dict[_Key, _Amount]) -> _Outcome:
    """Write *batch*; say what became of each entry.

    A store that applies a batch atomically and exactly once
    (``atomic_cost_increments``, the PostgreSQL store) takes it in one call.
    That call fails whole when the counters refuse any one entry, so a batch
    that fails for anything but the store being down is written again one
    entry at a time, each exactly once as well: an entry the counters refuse
    is then set aside, and cannot hold back everyone else's costs. Any other
    store is written one entry at a time with ``increment_user_cost``.
    """
    keys = list(batch)
    if getattr(store, "atomic_cost_increments", False) is not True:

        async def write_one(key: _Key) -> None:
            amount = batch[key]
            scopes = {}
            if amount.project_id is not None:
                scopes["project_id"] = amount.project_id
            # A native ledger writes its own counters with the exact counted
            # amount; a USD one writes the dollars it always has.
            if get_accounting_policy().native:
                increment, value = store.increment_user_cost_amount, amount.counted
            else:
                increment, value = store.increment_user_cost, amount.cost
            await increment(
                key[0],
                value,
                day=key[2],
                api_key_id=key[1],
                requests=amount.requests,
                **scopes,
            )

        return await _write_each(keys, write_one)
    try:
        await store.increment_user_costs([_increment(key, batch[key]) for key in keys])
    except CostWriteOutcomeUnknown as exc:
        return _Outcome(unknown=[(key, exc) for key in keys], error=exc)
    except Exception as exc:
        if len(keys) > 1 and not _unavailable(exc):

            async def write_alone(key: _Key) -> None:
                await store.increment_user_costs([_increment(key, batch[key])])

            return await _write_each(keys, write_alone)
        if _refused(exc):
            return _Outcome(refused=[(key, exc) for key in keys], error=exc)
        return _Outcome(failed=keys, error=exc)
    return _Outcome(applied=keys)


async def _write_each(keys: list[_Key], write_one: Callable[[_Key], Awaitable[None]]) -> _Outcome:
    """Write each entry on its own, until the store rather than an entry is failing."""
    outcome = _Outcome()
    for position, key in enumerate(keys):
        try:
            await write_one(key)
        except CostWriteOutcomeUnknown as exc:
            outcome.unknown.append((key, exc))
            outcome.error = exc
        except Exception as exc:
            outcome.error = exc
            if _refused(exc):
                outcome.refused.append((key, exc))
                continue
            outcome.failed.append(key)
            # The rest wait for the retry, without a futile try each.
            if _unavailable(exc) or (
                not outcome.applied and len(outcome.failed) >= _MAX_FAILURES_BEFORE_ANY_WRITE
            ):
                outcome.failed.extend(keys[position + 1 :])
                break
        else:
            outcome.applied.append(key)
    return outcome


def _increment(key: _Key, amount: _Amount) -> CostIncrement:
    """One counter entry: the counted amount, and on a native ledger its USD equivalent."""
    policy = get_accounting_policy()
    return CostIncrement(
        user_id=key[0],
        day=key[2],
        api_key_id=key[1],
        cost_usd=policy.as_usd(amount.counted),
        requests=amount.requests,
        project_id=amount.project_id,
        cost_amount=amount.counted if policy.native else None,
    )


def _sqlstate_class(exc: Exception) -> str:
    """The two-character class of the SQLSTATE a database error carries, or ''."""
    sqlstate = getattr(exc, "sqlstate", None)
    return sqlstate[:2] if isinstance(sqlstate, str) else ""


def _refused(exc: Exception) -> bool:
    """Whether the store refused the entry's own values, so it would fail again.

    A database's data or integrity error says so, and so does a driver that
    rejects an argument before sending it (asyncpg's ``DataError`` there is a
    ``ValueError``) or an arithmetic error on the amount.
    """
    return _sqlstate_class(exc) in _REFUSED_SQLSTATE_CLASSES or isinstance(
        exc, ValueError | ArithmeticError
    )


def _unavailable(exc: Exception) -> bool:
    """Whether the store itself failed, so no entry would go in just now."""
    return isinstance(exc, OSError | TimeoutError) or (
        _sqlstate_class(exc) in _UNAVAILABLE_SQLSTATE_CLASSES
    )


def _by_error(entries: list[tuple[_Key, Exception]]) -> list[tuple[Exception, list[_Key]]]:
    """*entries* grouped by the error each met, in order."""
    groups: dict[int, tuple[Exception, list[_Key]]] = {}
    for key, error in entries:
        groups.setdefault(id(error), (error, []))[1].append(key)
    return list(groups.values())


#: Alerts sent and still running, kept so they are not collected mid-send.
_ALERTS: set[asyncio.Task[Any]] = set()


def _send_alert(send: Callable[[Any], Awaitable[Any]]) -> None:
    """Page the configured channel, if any, without waiting: ``send(alerts)`` in a task.

    *send* calls ``alert_slack`` on the :mod:`serving.observability.alerts`
    module it is handed, each call site with its own literal key, as the
    alert-type catalog requires. Imported on use, which keeps this module's
    imports light; a failed send is logged, never raised.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from serving.observability import alerts

    async def run() -> None:
        try:
            await send(alerts)
        except Exception:
            # The log record already holds everything; the page is extra.
            logger.warning("Sending a cost counter alert failed", exc_info=True)

    task = loop.create_task(run())
    _ALERTS.add(task)
    task.add_done_callback(_ALERTS.discard)
    detached_tasks.track(task, name="alert_send")


# Per store, weakly: a store that goes away (tests make many) takes its
# ledger with it.
_LEDGERS: weakref.WeakKeyDictionary[Any, SpendLedger] = weakref.WeakKeyDictionary()
# For a store that cannot be weakly referenced; kept, with the store, for good.
_PINNED: dict[int, tuple[Any, SpendLedger]] = {}


def ledger_for(store: Any) -> Any:
    """The ledger of operational store *store*, made on first use; None without a store.

    The per-process :class:`SpendLedger`, or -- in a process that runs the
    shared mode (:mod:`serving.shared_spend.mode`) on a store that supports it
    -- its :class:`~serving.shared_spend.ledger.SharedSpendLedger`, which
    records and checks alike.
    """
    if store is None:
        return None
    from serving.shared_spend import mode as spend_mode

    if spend_mode.is_shared():
        # Fails closed on a store that cannot keep it: never the per-process
        # ledger behind a shared mode's back.
        from serving.shared_spend import ledger as shared_ledger

        return shared_ledger.ledger_for(store)
    try:
        ledger = _LEDGERS.get(store)
        if ledger is None:
            ledger = _LEDGERS[store] = SpendLedger(store)
        return ledger
    except TypeError:
        pinned = _PINNED.get(id(store))
        if pinned is None or pinned[0] is not store:
            pinned = _PINNED[id(store)] = (store, SpendLedger(store))
        return pinned[1]


def handoff_report() -> list[dict[str, Any]]:
    """Every ledger's :meth:`SpendLedger.handoff_report` (or the shared ledger's), now."""
    from serving.shared_spend import ledger as shared_ledger

    ledgers = [
        *_LEDGERS.values(),
        *(ledger for _, ledger in _PINNED.values()),
        *shared_ledger.all_ledgers(),
    ]
    return [ledger.handoff_report() for ledger in ledgers]


async def flush_all(timeout: float = 10.0) -> None:
    """Write every store's recorded costs before shutdown; report what could not be.

    Called by the application's shutdown while the stores can still write. A
    shared ledger that sent everything then records that this process will
    send nothing more (:meth:`serving.shared_spend.ledger.SharedSpendLedger.close`).
    """
    from serving.shared_spend import ledger as shared_ledger

    ledgers = [
        *_LEDGERS.values(),
        *(ledger for _, ledger in _PINNED.values()),
        *shared_ledger.all_ledgers(),
    ]
    for ledger in ledgers:
        with contextlib.suppress(Exception):
            await ledger.flush(timeout)
        cost, requests = ledger.unwritten()
        policy = get_accounting_policy()
        # The ledger amount stated natively too, on a native ledger.
        native = {"amount": str(cost), "currency": policy.currency} if policy.native else {}
        if requests:
            logger.error(
                "Shutting down with cost not written to the counters",
                extra={
                    "event": "cost_counter_unwritten_at_shutdown",
                    "cost_usd": round(float(policy.as_usd(cost)), 6),
                    **native,
                    "requests": requests,
                },
            )
        holdings = getattr(ledger, "holdings", None)
        held = holdings() if holdings is not None else {}
        if any(held.values()):
            # Shared reservations this process could not settle stay counted in
            # the database, for an operator (python -m serving.shared_spend).
            logger.error(
                "Shutting down with shared spend reservations not settled",
                extra={
                    "event": "spend_reservations_unsettled_at_shutdown",
                    "held": held,
                    "amount_usd": round(float(policy.as_usd(cost)), 6),
                    **native,
                },
            )
        close = getattr(ledger, "close", None)
        if close is not None and not await close():
            # Not recorded as closed: its settlement receipts are kept, and
            # what it still holds stays counted, until an operator says this
            # process is gone (python -m serving.shared_spend reconcile).
            logger.error(
                "Shutting down without recording this process's spend closure",
                extra={"event": "spend_instance_not_closed"},
            )
