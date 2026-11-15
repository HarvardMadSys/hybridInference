"""Shared spend admission on every scope it checks, against real PostgreSQL.

``test_shared_spend_postgres`` contends on one account's daily quota. This
module covers the rest of what an admission checks: the account's, the key's
and the project's day and month, each as settled counters plus every
process's open reservations; different accounts sharing one project; what a
settlement adds to each counter; UTC day and month boundaries; and how an
admission is bounded when its scope is locked, its pool is exhausted or the
database refuses writes.

The budget policy is the existing one (see that module's docstring): $1.00
free and $0.30 asked fits four times -- $0.30, $0.30, $0.30, then $0.10.

The UTC day is pinned (``clock``): every reading the ledgers make of it goes
through :func:`serving.spend_ledger.utc_day`. Interleavings are pinned with
the ledgers' test pauses and by watching ``pg_locks``, never by sleeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio

from serving.shared_spend import ledger as shared_ledger
from serving.shared_spend.ledger import SharedSpendLedger, SpendAdmissionUnavailable
from serving.shared_spend.store import (
    LOCK_CLASS_PROJECT,
    AdmissionRequest,
    CostLine,
    SettlementBatch,
    SharedSpendStore,
    scope_lock_keys,
    usd,
)
from serving.spend_ledger import SpendCheck
from serving.storage.pools import DatabaseBusyError, request_connection
from tests.fixtures import postgres
from tests.integration.storage import test_shared_spend_postgres as _base

db = _base.db
Gate = _base.Gate
_until = _base._until
_advisory_waiters = _base._advisory_waiters
_rows = _base._rows
_members = _base._members
_charge = _base._charge
end_quarantined = _base.end_quarantined

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PREFIX = _base.PREFIX  # "u-ss-": accounts the fixture wipes
PROJECT = "p-ss-"  # projects the fixture wipes
KEY = 7_310_001  # key ids: their counters are wiped by their account

#: Large enough never to bind: the account quota when a test is about another scope.
UNBOUNDED = 1000.0

D = Decimal


@pytest.fixture
def clock(monkeypatch):
    """The UTC day every ledger reading sees: ``clock.day``, changed by the test."""
    state = SimpleNamespace(day="2031-03-15")
    monkeypatch.setattr("serving.spend_ledger.utc_day", lambda now=None: state.day)
    return state


def limits(
    ledger,
    user: str,
    *,
    key_id: int | None = None,
    project_id: str | None = None,
    daily: float = UNBOUNDED,
    monthly: float | None = None,
    key_daily: float | None = None,
    key_monthly: float | None = None,
    project_daily: float | None = None,
    project_monthly: float | None = None,
    spent_today: float = 0.0,
) -> SpendCheck:
    """A gate's check with any of the six ceilings; *spent_today* is what the gate read."""
    return SpendCheck(
        ledger=ledger,
        user_id=user,
        key_id=key_id,
        read_at=time.monotonic(),
        account_quota_usd=daily,
        account_spent_usd=spent_today,
        account_quota_monthly_usd=monthly,
        key_budget_daily_usd=key_daily,
        key_budget_monthly_usd=key_monthly,
        project_id=project_id,
        project_budget_daily_usd=project_daily,
        project_budget_monthly_usd=project_monthly,
    )


async def _settle_plain(ledger, *lines: CostLine) -> None:
    """Add *lines* to the counters through the real settlement statement."""
    result = await ledger._backend.settle(SettlementBatch.make(ledger.instance_id, [], lines))
    assert result.outcome == "applied"


async def _probe(ledger, user: str, *, day: str, key_id=None, project_id=None):
    """What one admission on *day* reads under its locks, through the real statements."""
    seen = []

    def decide(snapshot):
        seen.append(snapshot)
        return None

    request = AdmissionRequest(
        reservation_id=f"sr_probe_{time.monotonic_ns()}",
        instance_id=ledger.instance_id,
        user_id=user,
        api_key_id=key_id,
        project_id=project_id,
        requested_usd=D("0.01"),
        check_day=day,
        operation="probe",
        check_key=key_id is not None,
        check_project=project_id is not None,
    )
    async with request_connection(ledger._backend.request_pool) as conn:
        await ledger._backend.admit_on(conn, request, decide, budget_ms=3000, on_xid=lambda x: None)
    return seen[0]


async def _amounts(ledger, make_check, asks) -> list:
    """Admit *asks* in order: what each reserved, or the refusal's (scope, window)."""
    out = []
    for ask in asks:
        reservation, refusal = await ledger.admit(make_check(), ask)
        out.append(reservation.amount if reservation else (refusal.scope, refusal.window))
    return out


# -- every ceiling: settled counters plus open reservations -------------------

# For each ceiling: the check's budget arguments, the scope of a cost line, and
# the refusal it names. A monthly case puts its settled cost on an earlier day
# of the month (the day's ceiling would not see it); a daily case puts extra
# cost on an earlier day that its ceiling must not count.
CEILINGS = {
    "account-day": ({"daily": 1.0}, "account", "day"),
    "account-month": ({"monthly": 1.0}, "account", "month"),
    "key-day": ({"key_daily": 1.0}, "key", "day"),
    "key-month": ({"key_monthly": 1.0}, "key", "month"),
    "project-day": ({"project_daily": 1.0}, "project", "day"),
    "project-month": ({"project_monthly": 1.0}, "project", "month"),
}


@pytest.mark.parametrize("ceiling", list(CEILINGS))
async def test_each_ceiling_counts_settled_cost_and_every_processes_reservations(
    db, clock, ceiling
):
    budgets, scope, window = CEILINGS[ceiling]
    today, earlier = clock.day, clock.day[:8] + "02"
    project = PROJECT + ceiling
    # The settled cost and the other process's reservation belong to the same
    # account for the account and key ceilings (a key is one account's), and
    # to another account sharing the project for the project ceilings.
    owner = PREFIX + ceiling
    other = owner if scope in ("account", "key") else PREFIX + ceiling + "-other"
    key = KEY if scope == "key" else None

    def make(ledger, user):
        return limits(
            ledger,
            user,
            key_id=key,
            project_id=project if scope == "project" else None,
            **budgets,
        )

    settled_on = earlier if window == "month" else today
    lines = [CostLine(other, settled_on, key, usd(0.40), 1, None, project)]
    if window == "day":
        lines.append(CostLine(other, earlier, key, usd(5.0), 1, None, project))
    await _settle_plain(db.b, *lines)
    held, _ = await db.b.admit(make(db.b, other), 0.30)  # another process, still running
    assert held.amount == D("0.3")

    # $0.40 settled + $0.30 reserved elsewhere: $0.30 left, then the partial $0.10.
    got = await _amounts(db.a, lambda: make(db.a, owner), [0.20, 0.30, 0.30])
    assert got == [D("0.2"), D("0.1"), (scope, window)]
    _, refusal = await db.a.admit(make(db.a, owner), 0.30)
    assert refusal.spent_usd == pytest.approx(1.0)

    # The settled cost alone, without the other process's reservation, is not
    # what refused it: once that reservation is released, its $0.30 is free.
    held.release()
    assert await db.b.flush(5)
    assert await _amounts(db.a, lambda: make(db.a, owner), [0.30, 0.30]) == [
        D("0.3"),
        (scope, window),
    ]


# -- one project, many accounts --------------------------------------------------


async def test_accounts_sharing_a_project_wait_on_its_lock_and_share_its_headroom(db, clock):
    project = PROJECT + "shared"
    u1, u2 = PREFIX + "proj-1", PREFIX + "proj-2"

    def make(ledger, user):
        return limits(ledger, user, project_id=project, project_daily=1.0)

    # u1's admission holds the project's lock; u2 -- another account, so no
    # account lock in common -- waits for it in the database.
    gate = Gate("inserted")
    db.a.pause = gate
    first = asyncio.create_task(db.a.admit(make(db.a, u1), 0.80))
    await asyncio.wait_for(gate.reached.wait(), 5)
    second = asyncio.create_task(db.b.admit(make(db.b, u2), 0.80))
    await _until(lambda: _waiting(db.pool))
    assert not second.done()
    gate.go.set()
    (r1, _), (r2, _) = await first, await second
    # Read before the wait, u2 would have seen $1.00 free and reserved $0.80.
    assert (r1.amount, r2.amount) == (D("0.8"), D("0.2"))
    db.a.pause = None

    # A burst from four accounts in two processes into a fresh project: the
    # project's $1.00 is shared exactly, whichever accounts win.
    burst = PROJECT + "burst"
    users = [PREFIX + f"burst-{i}" for i in range(4)]
    results = await asyncio.gather(
        *[
            (db.a if i % 2 else db.b).admit(
                limits(
                    db.a if i % 2 else db.b,
                    users[i % 4],
                    project_id=burst,
                    project_daily=1.0,
                ),
                0.30,
            )
            for i in range(20)
        ]
    )
    admitted = sorted(r.amount for r, _ in results if r is not None)
    assert admitted == [D("0.1"), D("0.3"), D("0.3"), D("0.3")]
    assert {refusal.scope for r, refusal in results if refusal is not None} == {"project"}
    rows = [r for r in await _rows(db.pool) if r["project_id"] == burst]
    assert sum(r["amount_usd"] for r in rows) == D("1")


async def _waiting(pool) -> bool:
    return await _advisory_waiters(pool) >= 1


async def test_independent_projects_neither_wait_on_nor_spend_each_others_headroom(db, clock):
    p1, p2 = PROJECT + "iso-1", PROJECT + "iso-2"
    u1, u2 = PREFIX + "iso-1", PREFIX + "iso-2"

    # p1 is full.
    full, _ = await db.a.admit(limits(db.a, u1, project_id=p1, project_daily=1.0), 1.0)
    assert full.amount == D("1")

    # While an admission into p1 holds p1's lock, p2 is admitted at once and in full.
    gate = Gate("locked")
    db.a.pause = gate
    held = asyncio.create_task(db.a.admit(limits(db.a, u1, project_id=p1, project_daily=1.0)))
    await asyncio.wait_for(gate.reached.wait(), 5)
    got = await asyncio.wait_for(
        _amounts(db.b, lambda: limits(db.b, u2, project_id=p2, project_daily=1.0), [0.3] * 5), 5
    )
    assert got == [D("0.3"), D("0.3"), D("0.3"), D("0.1"), ("project", "day")]
    gate.go.set()
    _, refusal = await held
    assert (refusal.scope, refusal.window) == ("project", "day")


# -- settlement: where each cost is counted ----------------------------------------


async def test_settlement_adds_each_cost_to_the_account_key_and_project_counters(db, clock):
    user, other = PREFIX + "attr", PREFIX + "attr-other"
    project = PROJECT + "attr"
    month = clock.day[:7]

    def make(ledger):
        return limits(
            ledger, user, key_id=KEY, project_id=project, key_daily=10.0, project_daily=10.0
        )

    settled, _ = await db.a.admit(make(db.a), 0.30)
    _charge(db.a, settled, 0.25, project=project, key=KEY)
    _charge(db.a, settled, 0.15, project=project, key=KEY)
    small, _ = await db.a.admit(make(db.a), 0.30)
    _charge(db.a, small, 0.05, project=project, key=KEY)
    no_scope, _ = await db.a.admit(limits(db.a, user), 0.30)
    _charge(db.a, no_scope, 0.10)
    db.a.record(other, 0.07, api_key_id=KEY + 1)  # a plain increment, another key
    settled.release()
    small.release()
    no_scope.release()
    assert await db.a.flush(5)

    async with db.pool.acquire() as conn:
        account = await conn.fetch(
            "SELECT user_id, day, cost_usd, requests FROM user_daily_cost "
            "WHERE user_id = ANY($1::text[]) ORDER BY user_id",
            [user, other],
        )
        keys = await conn.fetch(
            "SELECT key_id, day, user_id, cost_usd, requests FROM api_key_daily_cost "
            "WHERE key_id = ANY($1::bigint[]) ORDER BY key_id",
            [KEY, KEY + 1],
        )
        projects = await conn.fetch(
            "SELECT period, cost_usd, requests FROM spend_counters "
            "WHERE scope_id = $1 ORDER BY period",
            project,
        )
    # $0.25 + $0.15 of one request, $0.05 of another, $0.10 without a key or
    # project; one request per cost recorded.
    assert [tuple(r) for r in account] == [
        (user, clock.day, D("0.55"), 4),
        (other, clock.day, D("0.07"), 1),
    ]
    assert [tuple(r) for r in keys] == [
        (KEY, clock.day, user, D("0.45"), 3),
        (KEY + 1, clock.day, other, D("0.07"), 1),
    ]
    assert [tuple(r) for r in projects] == [
        (f"d:{clock.day}", D("0.45"), 3),
        (f"m:{month}", D("0.45"), 3),
    ]

    # Every scope's next admission reads those counters, and nothing held:
    # each request gave back what it had reserved beyond its cost.
    snap = await _probe(db.b, user, day=clock.day, key_id=KEY, project_id=project)
    assert (snap.account_day, snap.key_day, snap.project_day) == (D("0.55"), D("0.45"), D("0.45"))
    assert (snap.account_month, snap.key_month, snap.project_month) == (
        D("0.55"),
        D("0.45"),
        D("0.45"),
    )
    assert (snap.reserved_account_day, snap.reserved_key_day, snap.reserved_project_day) == (
        D(0),
        D(0),
        D(0),
    )
    assert sorted((await _members(db.pool)).values()) == ["settled", "settled", "settled"]


# -- UTC day and month boundaries ---------------------------------------------------


def _gate_extra(ledger, user: str) -> tuple[float, float]:
    """What *ledger*'s gate adds to the account's day and month spend, from its own memory."""
    extra = ledger._local_extra(user, None, None, reserved=True)
    return (pytest.approx(extra[0]), pytest.approx(extra[1]))


async def test_at_midnight_an_active_reservation_moves_on_and_a_quarantined_one_stays(db, clock):
    """A running reservation counts against the current day; one that ended, against its own.

    Both in the database (what every process's admission reads) and in the
    memory of the process that holds it (what its gate adds before admission):
    the two must name the same day, or the gate of the process that served a
    request refuses on later days what the admission, and every other
    process's gate, lets through.
    """
    user = PREFIX + "midnight"
    clock.day = "2031-01-31"

    def make(ledger, **budgets):
        return limits(ledger, user, daily=1.0, monthly=1.5, **budgets)

    await _settle_plain(db.a, CostLine(user, clock.day, None, usd(0.20)))
    running, _ = await db.a.admit(make(db.a), 0.30)  # still running at midnight
    ended, _ = await db.a.admit(make(db.a), 0.30)
    await end_quarantined(db.a, ended, 0.10)  # ended on Jan 31; the counters refused its cost

    jan = await _probe(db.b, user, day="2031-01-31")
    assert (jan.account_day, jan.reserved_account_day) == (D("0.2"), D("0.6"))
    # The holder's gate: the running one and the quarantined one, both today.
    assert _gate_extra(db.a, user) == (0.6, 0.6)
    assert _gate_extra(db.b, user) == (0.0, 0.0)

    # Feb 1: the running reservation counts against the new day and month; the
    # one that ended counts against the day and month it ended in, so neither
    # it nor Jan 31's counter is in February's.
    clock.day = "2031-02-01"
    feb = await _probe(db.b, user, day=clock.day)
    assert (feb.account_day, feb.reserved_account_day) == (D(0), D("0.3"))
    assert (feb.account_month, feb.reserved_account_month) == (D(0), D("0.3"))
    assert _gate_extra(db.a, user) == (0.3, 0.3)  # as the database: the running one only
    # With $0.60 of the $1.00 spent today, both gates pass the caller: the
    # holder's adds the running $0.30 and nothing of Jan 31. (Counting that
    # liability today as well, the gate of the process that holds it refused
    # what the admission and every other process's gate let through.)
    for ledger in (db.b, db.a):
        assert ledger.refusal(limits(ledger, user, daily=1.0, spent_today=0.60)) is None
    assert (await db.b.admit(make(db.b), 5.0))[0].amount == D("0.7")

    # It ends now, its cost refused as well: a liability of Feb 1, not of the
    # day it was admitted.
    await end_quarantined(db.a, running, 0.05)
    rows = {r["reservation_id"]: r for r in await _rows(db.pool, user)}
    assert (
        rows[running.reservation_id]["state"],
        rows[running.reservation_id]["liability_day"],
    ) == (
        "quarantined",
        "2031-02-01",
    )
    assert rows[ended.reservation_id]["liability_day"] == "2031-01-31"
    assert _gate_extra(db.a, user) == (0.3, 0.3)  # Feb 1's liability, today

    # Feb 2: Feb 1's liability counts against the month, not the day.
    clock.day = "2031-02-02"
    snap = await _probe(db.b, user, day=clock.day)
    open_feb = D("0.7")  # the reservation admitted on Feb 1 above, still active
    assert (snap.account_day, snap.reserved_account_day) == (D(0), open_feb)
    assert (snap.account_month, snap.reserved_account_month) == (D(0), open_feb + D("0.3"))
    assert _gate_extra(db.a, user) == (0.0, 0.3)
    # Day: $1.00 - $0.70 = $0.30 left; month: $1.50 - $1.00 = $0.50 left.
    assert (await db.a.admit(make(db.a), 5.0))[0].amount == D("0.3")
    # Nothing expired: every reservation is still there, and still reported.
    assert len(await _rows(db.pool, user)) == 4
    assert db.a.holdings()["quarantined"] == 2
    # January's no longer counts against anything: it left what every check
    # reads, and stays in what the process reports.
    assert list(db.a._past_liabilities) == [ended.reservation_id]
    assert list(db.a._liabilities) == [running.reservation_id]


async def test_a_liability_held_only_in_memory_counts_on_its_day_and_month_alone(db, clock):
    """An unaccounted reservation is in no table: its holder alone counts it, on its own day."""
    user = PREFIX + "unaccounted"
    clock.day = "2031-06-29"
    lost, _ = await db.a.admit(limits(db.a, user, daily=1.0, monthly=5.0), 0.30)
    _charge(db.a, lost, 0.40)
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM spend_reservations WHERE reservation_id = $1", lost.reservation_id
        )
    lost.release()
    assert await db.a.flush(5)
    assert lost.state == "unaccounted"

    def both(ledger):
        return (
            _gate_extra(ledger, user),
            pytest.approx(ledger._local_extra(user, None, None, reserved=False)[0]),
            pytest.approx(ledger._local_extra(user, None, None, reserved=False)[1]),
        )

    assert both(db.a) == ((0.4, 0.4), 0.4, 0.4)  # its day: the gate and the admission
    clock.day = "2031-06-30"
    assert both(db.a) == ((0.0, 0.4), 0.0, 0.4)  # the next day: its month only
    clock.day = "2031-07-01"
    assert both(db.a) == ((0.0, 0.0), 0.0, 0.0)  # the next month: nothing
    assert db.a.holdings()["unaccounted"] == 1  # still reported
    assert db.a.unwritten() == (pytest.approx(0.4), 1)


async def test_month_ceilings_of_key_and_project_turn_over_with_the_utc_month(db, clock):
    user = PREFIX + "month-scopes"
    project = PROJECT + "month-scopes"

    def make(ledger):
        return limits(
            ledger, user, key_id=KEY, project_id=project, key_monthly=1.0, project_monthly=1.0
        )

    # April: $0.70 settled on an earlier day, and a $0.30 liability of Apr 30
    # (a reservation whose cost the counters refused), fill both month
    # ceilings (the key's is checked first).
    clock.day = "2031-04-30"
    await _settle_plain(db.a, CostLine(user, "2031-04-03", KEY, usd(0.70), 1, None, project))
    ended, _ = await db.a.admit(make(db.a), 0.30)
    await end_quarantined(db.a, ended, 0.10, project=project, key=KEY)
    apr = await _probe(db.b, user, day=clock.day, key_id=KEY, project_id=project)
    assert (apr.key_day, apr.key_month, apr.project_day, apr.project_month) == (
        D(0),
        D("0.7"),
        D(0),
        D("0.7"),
    )
    assert (apr.reserved_key_month, apr.reserved_project_month) == (D("0.3"), D("0.3"))
    assert await _amounts(db.a, lambda: make(db.a), [0.30]) == [("key", "month")]

    clock.day = "2031-05-01"
    snap = await _probe(db.b, user, day=clock.day, key_id=KEY, project_id=project)
    assert (snap.key_month, snap.project_month) == (D(0), D(0))
    assert (snap.reserved_key_month, snap.reserved_project_month) == (D(0), D(0))
    assert (await db.b.admit(make(db.b), 5.0))[0].amount == D("1")


# -- the limits an admission applies -------------------------------------------------


async def test_limits_are_the_gates_and_spend_is_read_again_under_the_locks(db, clock):
    """The quota a check carries is the one applied; the spend it read is not.

    A quota changed after a gate read it applies from the next gate's check;
    reservations already admitted are not revoked by a lower quota. The spend
    is always the admission's own read, on the day the admission runs.
    """
    user = PREFIX + "limits"
    before = limits(db.a, user, daily=1.0)  # read at the gate, before the quota changed

    held, _ = await db.b.admit(limits(db.b, user, daily=1.0), 0.60)
    assert held.amount == D("0.6")

    # Lowered to $0.50: a new check is refused; the reservation stays.
    lowered = limits(db.a, user, daily=0.50)
    reservation, refusal = await db.a.admit(lowered, 0.30)
    assert reservation is None
    assert (refusal.limit_usd, refusal.spent_usd) == (0.50, pytest.approx(0.60))
    # The check read before the change still applies its own $1.00.
    assert (await db.a.admit(before, 0.30))[0].amount == D("0.3")
    # Raised to $2.00: the next check fits more at once.
    assert (await db.a.admit(limits(db.a, user, daily=2.0), 5.0))[0].amount == D("1.1")
    assert len(await _rows(db.pool, user)) == 3

    # A gate that read a full Mar 15 and admits after midnight: the admission
    # reads Mar 16, and the reservation is Mar 16's.
    late = PREFIX + "limits-midnight"
    await _settle_plain(db.a, CostLine(late, clock.day, None, usd(0.99)))
    stale = limits(db.a, late, daily=1.0, spent_today=0.99)
    clock.day = "2031-03-16"
    reservation, _ = await db.a.admit(stale, 0.30)
    assert reservation.amount == D("0.3")
    assert [r["check_day"] for r in await _rows(db.pool, late)] == ["2031-03-16"]


# -- bounded under contention, exhaustion and refusal ---------------------------------


def _account_lock(user: str) -> list[tuple[int, int]]:
    return scope_lock_keys(user, None, None)


def _project_lock(project: str) -> list[tuple[int, int]]:
    return [k for k in scope_lock_keys("", None, project) if k[0] == LOCK_CLASS_PROJECT]


@contextlib.asynccontextmanager
async def _holding(keys: list[tuple[int, int]]):
    """Hold admission locks from another session, as a stuck admission would."""
    conn = await postgres.connect()
    try:
        await conn.execute("BEGIN")
        for cls, obj in keys:
            await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", cls, obj)
        yield
    finally:
        try:
            await conn.close(timeout=5)  # ends the transaction, and its locks
        except Exception:
            conn.terminate()


@pytest.fixture
def short_budget(monkeypatch):
    """A half-second admission budget, so a bounded wait is short to watch."""
    monkeypatch.setattr(shared_ledger, "ADMISSION_BUDGET_SECONDS", 0.5)
    return 0.5


async def _bounded(awaitables, budget: float, *, started: float | None = None) -> None:
    """Every one of *awaitables* fails as a 503 (``DatabaseBusyError``) within *budget*.

    ``SpendAdmissionUnavailable`` is the ledger's own deadline or a database
    error; a pool that had no connection in time raises its base class.
    """
    started = time.monotonic() if started is None else started
    results = await asyncio.wait_for(
        asyncio.gather(*awaitables, return_exceptions=True), budget + 5
    )
    elapsed = time.monotonic() - started
    assert all(isinstance(r, DatabaseBusyError) for r in results), results
    assert elapsed < budget + 1.0, elapsed


async def test_a_locked_scope_fails_in_bounded_time_and_other_scopes_proceed(
    db, clock, short_budget
):
    hot, cold = PREFIX + "hot", PREFIX + "cold"
    project, other_project = PROJECT + "hot", PROJECT + "cold"

    async with _holding(_account_lock(hot)):
        await _bounded([db.a.admit(limits(db.a, hot), 0.30)], short_budget)
        # Another account proceeds while hot's lock is held.
        assert (await db.b.admit(limits(db.b, cold), 0.30))[0].amount == D("0.3")

    async with _holding(_project_lock(project)):
        # Any account of the locked project waits, and gives up in time ...
        await _bounded(
            [db.b.admit(limits(db.b, hot, project_id=project, project_daily=1.0), 0.30)],
            short_budget,
        )
        # ... while the same account in another project, or with no project
        # budget, is admitted.
        reservation, _ = await db.b.admit(
            limits(db.b, cold, project_id=other_project, project_daily=1.0), 0.30
        )
        assert reservation.amount == D("0.3")
        assert (await db.b.admit(limits(db.b, cold, project_id=project), 0.30))[0] is not None

    # Nothing of the attempts that gave up is held, here or in the database.
    assert await _rows(db.pool, hot) == []
    assert db.a.holdings()["abandoned"] == db.b.holdings()["abandoned"] == 0
    assert (await db.a.admit(limits(db.a, hot), 0.30))[0].amount == D("0.3")


@pytest_asyncio.fixture
async def small_pools():
    """A ledger whose request pool has two connections, for exhausting it."""
    general = await postgres.create_pool(min_size=1, max_size=2)
    request = await postgres.create_pool(min_size=2, max_size=2)
    backend = SharedSpendStore(general, request_pool=request, fence_secret=postgres.FENCE_SECRET)
    ns = SimpleNamespace(
        ledger=SharedSpendLedger(backend, instance_id="inst-small"),
        request=request,
        general=general,
    )
    try:
        yield ns
    finally:
        if ns.ledger._writer is not None:
            ns.ledger._writer.cancel()
        for pool in (request, general):
            try:
                await asyncio.wait_for(pool.close(), 5)
            except TimeoutError:
                pool.terminate()


async def test_a_hot_account_holds_one_request_connection_and_others_still_get_one(
    db, clock, small_pools, short_budget
):
    ledger = small_pools.ledger
    hot, cold = PREFIX + "pool-hot", PREFIX + "pool-cold"
    async with _holding(_account_lock(hot)):
        started = time.monotonic()
        waiting = [asyncio.create_task(ledger.admit(limits(ledger, hot), 0.30)) for _ in range(8)]
        await _until(lambda: _waiting(db.pool))
        # Eight admissions of the hot account, one waiting in the database:
        # the rest wait in this process, so the pool's second connection is free.
        assert await _advisory_waiters(db.pool) == 1
        assert (await ledger.admit(limits(ledger, cold), 0.30))[0].amount == D("0.3")
        await _bounded(waiting, short_budget, started=started)
    assert await _rows(db.pool, hot) == []
    assert ledger.holdings()["abandoned"] == 0


async def test_an_exhausted_request_pool_refuses_in_bounded_time_and_writes_nothing(
    db, clock, small_pools, short_budget
):
    ledger = small_pools.ledger
    user = PREFIX + "exhausted"
    async with contextlib.AsyncExitStack() as stack:
        for _ in range(2):
            await stack.enter_async_context(small_pools.request.acquire())
        await _bounded([ledger.admit(limits(ledger, user), 0.30)], short_budget)
    assert await _rows(db.pool, user) == []
    assert ledger.holdings()["abandoned"] == 0
    assert (await ledger.admit(limits(ledger, user), 0.30))[0].amount == D("0.3")


async def test_a_read_only_database_refuses_admission_and_keeps_settlements_queued(
    db, clock, short_budget
):
    """Writes refused (a read-only primary, a standby) lose nothing and admit nothing."""
    user = PREFIX + "read-only"
    read_only = await postgres.create_pool(
        min_size=1, max_size=2, server_settings={"default_transaction_read_only": "on"}
    )
    writable = db.a._backend.pool
    try:
        refused = SharedSpendLedger(
            SharedSpendStore(read_only, fence_secret=postgres.FENCE_SECRET),
            instance_id="inst-ro",
        )
        with pytest.raises(SpendAdmissionUnavailable):  # its INSERT refused, read-only
            await refused.admit(limits(refused, user), 0.30)
        assert await refused.flush(5)  # the attempt is known aborted: nothing to release
        assert refused.fully_settled()
        assert await _rows(db.pool, user) == []

        # A request admitted while writable, settled while the writes are refused.
        reservation, _ = await db.a.admit(limits(db.a, user, daily=1.0), 0.30)
        _charge(db.a, reservation, 0.40)
        db.a._backend.pool = read_only
        reservation.release()
        assert not await db.a.flush(1)
        assert db.a.holdings()["queued"] == 1 and db.a.holdings()["quarantined"] == 0
        assert db.a.unwritten() == (pytest.approx(0.40), 1)
        assert [r["state"] for r in await _rows(db.pool, user)] == ["active"]
        # Still counted: $0.40 known against $0.30 reserved, here and elsewhere.
        assert (await db.a.admit(limits(db.a, user, daily=1.0), 1.0))[0].amount == D("0.6")

        db.a._backend.pool = writable
        assert await db.a.flush(5)
        assert await _base._account(db.pool, user, clock.day) == D("0.4")
        assert [r["amount_usd"] for r in await _rows(db.pool, user)] == [D("0.6")]
    finally:
        db.a._backend.pool = writable
        await asyncio.wait_for(read_only.close(), 5)
