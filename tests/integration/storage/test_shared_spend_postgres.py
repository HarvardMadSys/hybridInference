"""Shared spend accounting against real PostgreSQL: admission, settlement, recovery.

Two ledgers on two pools with two instance ids stand for two gateway
processes (one test also runs two real OS processes). Interleavings are pinned
with the ledgers' test pauses and by watching ``pg_locks``, never by sleeping.

The budget policy is the existing one: a request is refused only when not even
$0.01 fits, and otherwise reserves its estimate clamped to what is left. So
four admissions of $0.30 fit into $1.00 -- $0.30, $0.30, $0.30 and $0.10 --
and what is reserved is an estimate, not a bound on what a request costs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from decimal import Decimal
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

from serving.shared_spend import mode as spend_mode
from serving.shared_spend.ledger import (
    AccountErased,
    ReservationConflict,
    SharedSpendLedger,
)
from serving.shared_spend.store import (
    AdmissionRequest,
    CostLine,
    Member,
    SettlementBatch,
    quantized_amount,
)
from serving.spend_ledger import SpendCheck, utc_day
from serving.storage.erasure_fence import fence_account_digest
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.utils import context as req_ctx
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PREFIX = "u-ss-"


async def _wipe(pool) -> None:
    async with pool.acquire() as conn:
        for table in (
            "spend_reservations",
            "spend_settlements",
            "spend_settlement_members",
            "spend_unidentified_writes",
            "spend_instance_closures",
            "spend_reconciliations",
        ):
            await conn.execute(f"DELETE FROM {table}")
        # Request-log rows and registrations the reconciliation tests make.
        await conn.execute("DELETE FROM api_logs WHERE user_id LIKE 'u-ss-%'")
        await conn.execute("DELETE FROM config_instances WHERE instance_id LIKE 'inst-%'")
        await conn.execute("DELETE FROM user_daily_cost WHERE user_id LIKE 'u-ss-%'")
        await conn.execute("DELETE FROM api_key_daily_cost WHERE user_id LIKE 'u-ss-%'")
        await conn.execute("DELETE FROM spend_counters WHERE scope_id LIKE 'p-ss-%'")
        await conn.execute("UPDATE spend_accounting SET mode = 'local' WHERE id")


@pytest_asyncio.fixture
async def db():
    pool_a = await postgres.create_pool(min_size=1, max_size=6)
    pool_b = await postgres.create_pool(min_size=1, max_size=6)
    store_a = PostgresOperationalStore(pool_a, fence_secret=postgres.FENCE_SECRET)
    store_b = PostgresOperationalStore(pool_b, fence_secret=postgres.FENCE_SECRET)
    await _wipe(pool_a)
    erased: list[str] = []
    ns = SimpleNamespace(
        pool=pool_a,
        a=SharedSpendLedger(store_a.shared_spend_backend(), instance_id="inst-a"),
        b=SharedSpendLedger(store_b.shared_spend_backend(), instance_id="inst-b"),
        store=store_a,
        erased=erased,
    )
    try:
        yield ns
    finally:
        # Bounded, and the pools ended whatever happens: a test that failed
        # with a connection out, or a transaction open, fails here and does
        # not hang the run (a pool's close() waits for every connection).
        try:
            async with asyncio.timeout(30):
                for ledger in (ns.a, ns.b):
                    if ledger._writer is not None:
                        ledger._writer.cancel()
                        with contextlib.suppress(BaseException):
                            await ledger._writer
                    for timer in (ledger._retry, ledger._mark_retry):
                        if timer is not None:
                            timer.cancel()
                async with pool_a.acquire() as conn:
                    for digest in erased:
                        await conn.execute(
                            "DELETE FROM erasure_fence WHERE account_digest = $1", digest
                        )
                await _wipe(pool_a)
        finally:
            await close_pool(pool_a)
            await close_pool(pool_b)


async def close_pool(pool) -> None:
    """Close *pool*; end it where a connection is still out after a few seconds."""
    try:
        await asyncio.wait_for(pool.close(), 5)
    except TimeoutError:
        pool.terminate()


async def every_place_is_free(pool) -> None:
    """Take every connection *pool* can hold at once: none of its places has been lost."""
    async with asyncio.timeout(5), contextlib.AsyncExitStack() as stack:
        for _ in range(pool.get_max_size()):
            await stack.enter_async_context(pool.acquire())


def check(
    ledger,
    user: str,
    *,
    daily: float = 1.0,
    monthly: float | None = None,
    key_id: int | None = None,
    key_daily: float | None = None,
    project_id: str | None = None,
    project_daily: float | None = None,
) -> SpendCheck:
    return SpendCheck(
        ledger=ledger,
        user_id=user,
        key_id=key_id,
        read_at=time.monotonic(),
        account_quota_usd=daily,
        account_spent_usd=0.0,
        account_quota_monthly_usd=monthly,
        key_budget_daily_usd=key_daily,
        project_id=project_id,
        project_budget_daily_usd=project_daily,
    )


class Gate:
    """A test pause that holds the first time *step* is reached, until released."""

    def __init__(self, step: str) -> None:
        self.step = step
        self.reached = asyncio.Event()
        self.go = asyncio.Event()

    async def __call__(self, step: str) -> None:
        if step == self.step and not self.go.is_set():
            self.reached.set()
            await self.go.wait()


async def _until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def _advisory_waiters(pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        )


async def _account(pool, user: str, day: str | None = None) -> Decimal:
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT cost_amount FROM user_daily_cost WHERE user_id = $1 AND day = $2",
            user,
            day or utc_day(),
        )
    return Decimal(value or 0)


async def _rows(pool, user: str | None = None) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM spend_reservations WHERE $1::text IS NULL OR user_id = $1 "
            "ORDER BY created_at",
            user,
        )
    return [dict(row) for row in rows]


async def _members(pool) -> dict[str, str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT reservation_id, outcome FROM spend_settlement_members")
    return {row["reservation_id"]: row["outcome"] for row in rows}


def _charge(ledger, reservation, cost: float, *, project: str | None = None, key=None):
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation, req_ctx.PROJECT_ID: project}):
        ledger.record(reservation.user_id, cost, api_key_id=key)


class RefusesCosts:
    """The real backend, whose counters refuse any batch that carries a cost (as an overflow does)."""

    def __init__(self, backend) -> None:
        self._backend = backend

    def __getattr__(self, name):
        return getattr(self._backend, name)

    async def settle(self, batch, **kwargs):
        if batch.all_costs():
            raise asyncpg.exceptions.NumericValueOutOfRangeError("numeric field overflow")
        return await self._backend.settle(batch, **kwargs)


async def end_quarantined(ledger, reservation, cost: float, *, project=None, key=None) -> None:
    """End *reservation* with *cost* the counters refuse: quarantined, its cost in no counter."""
    _charge(ledger, reservation, cost, project=project, key=key)
    real, ledger._backend = ledger._backend, RefusesCosts(ledger._backend)
    try:
        reservation.release()
        assert await ledger.flush(5)
    finally:
        ledger._backend = real
    assert reservation.state == "quarantined"


# -- admission contention -----------------------------------------------------


async def test_two_processes_share_one_accounts_headroom_exactly(db):
    user = PREFIX + "same"
    results = await asyncio.gather(
        *[
            (db.a if i % 2 else db.b).admit(check(db.a if i % 2 else db.b, user), 0.30)
            for i in range(20)
        ]
    )
    admitted = sorted(r.amount for r, refusal in results if r is not None)
    assert admitted == [Decimal("0.100000"), *[Decimal("0.300000")] * 3]
    assert sum(1 for r, refusal in results if refusal is not None) == 16
    rows = await _rows(db.pool, user)
    assert sum(row["amount"] for row in rows) == Decimal("1.000000")
    assert {row["instance_id"] for row in rows} <= {"inst-a", "inst-b"}


async def test_independent_accounts_do_not_wait_on_each_other(db):
    gate = Gate("locked")
    db.a.pause = gate
    held = asyncio.create_task(db.a.admit(check(db.a, PREFIX + "x"), 0.30))
    await asyncio.wait_for(gate.reached.wait(), 5)

    # Another account is admitted while x's locks are held.
    other, refusal = await asyncio.wait_for(db.b.admit(check(db.b, PREFIX + "y"), 0.30), 5)
    assert other is not None and refusal is None

    # The same account waits for the lock, in the database.
    same = asyncio.create_task(db.b.admit(check(db.b, PREFIX + "x"), 0.30))
    await _until(lambda: _waiting(db.pool))
    assert not same.done()
    gate.go.set()
    first, _ = await held
    second, _ = await same
    assert first.amount == second.amount == Decimal("0.300000")


async def _waiting(pool) -> bool:
    return await _advisory_waiters(pool) >= 1


async def test_an_admission_that_waited_for_the_lock_reads_what_its_holder_committed(db):
    user = PREFIX + "fresh"
    gate = Gate("inserted")
    db.a.pause = gate
    first = asyncio.create_task(db.a.admit(check(db.a, user, daily=0.40), 0.30))
    await asyncio.wait_for(gate.reached.wait(), 5)
    second = asyncio.create_task(db.b.admit(check(db.b, user, daily=0.40), 0.30))
    await _until(lambda: _waiting(db.pool))
    gate.go.set()
    r1, _ = await first
    r2, _ = await second
    # A read taken before the wait would have seen $0.40 free and reserved $0.30.
    assert (r1.amount, r2.amount) == (Decimal("0.300000"), Decimal("0.100000"))


# -- settlement racing admission ----------------------------------------------


async def _admit_and_finish(ledger, user: str, *, estimate: float, cost: float):
    reservation, _ = await ledger.admit(check(ledger, user, daily=10.0), estimate)
    _charge(ledger, reservation, cost)
    return reservation


async def test_naive_separate_reads_lose_a_cost_settled_between_them(db):
    """The hazard the one-statement read exists for, reproduced step by step."""
    user = PREFIX + "naive"
    reservation, _ = await db.a.admit(check(db.a, user, daily=10.0), 0.30)
    _charge(db.a, reservation, 0.50)
    gate = Gate("before_commit")
    db.a.settle_pause = gate
    reservation.release()
    await asyncio.wait_for(gate.reached.wait(), 5)

    async with db.pool.acquire() as conn:
        counters = await conn.fetchval(
            "SELECT COALESCE(SUM(cost_amount), 0) FROM user_daily_cost WHERE user_id = $1", user
        )
        gate.go.set()
        await db.a.flush(5)
        reserved = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM spend_reservations WHERE user_id = $1",
            user,
        )
    assert counters + reserved == 0  # the $0.50 is in neither read


async def _snapshot(ledger, user: str):
    """Run one admission's read through the real statements and return what it saw."""
    from serving.storage.pools import request_connection

    seen = []

    def decide(snapshot):
        seen.append(snapshot)
        return None

    request = AdmissionRequest(
        reservation_id=f"sr_probe_{time.monotonic_ns()}",
        instance_id=ledger.instance_id,
        user_id=user,
        api_key_id=None,
        project_id=None,
        requested_amount=Decimal("0.01"),
        check_day=utc_day(),
        operation="probe",
    )
    async with request_connection(ledger._backend.request_pool) as conn:
        await ledger._backend.admit_on(
            conn, request, decide, budget_ms=3000, on_xid=lambda x: None, pause=ledger.pause
        )
    return seen[0]


async def test_the_admission_read_sees_a_settlement_wholly_or_not_at_all(db):
    user = PREFIX + "race"
    reservation = await _admit_and_finish(db.a, user, estimate=0.30, cost=0.50)

    # Settlement written but not committed: the reservation, not the cost.
    gate = Gate("before_commit")
    db.a.settle_pause = gate
    reservation.release()
    await asyncio.wait_for(gate.reached.wait(), 5)
    before = await _snapshot(db.b, user)
    assert (before.account_day, before.reserved_account_day) == (Decimal(0), Decimal("0.3"))

    # Settlement commits while an admission holds its locks, before its read:
    # the read, taken after the locks, sees the cost and not the reservation.
    lock_gate = Gate("locked")
    db.b.pause = lock_gate
    probe = asyncio.create_task(_snapshot(db.b, user))
    await asyncio.wait_for(lock_gate.reached.wait(), 5)
    gate.go.set()
    assert await db.a.flush(5)
    lock_gate.go.set()
    after = await probe
    assert (after.account_day, after.reserved_account_day) == (Decimal("0.5"), Decimal(0))


async def test_a_cost_settled_between_an_admissions_read_and_its_decision_is_still_counted(db):
    """An admission adds to what it read what only this process knows -- at its decision.

    Between its read and its decision this process's own settlement can commit
    and be answered: the read was taken before the commit, so it has the
    reservation at its estimate and the counters without the cost; and by the
    decision the process no longer holds the request, so the part of its cost
    above the estimate, and any plain cost of the batch, were in neither. A
    request that cost the whole $1.00 limit then let the next one in.
    """
    user = PREFIX + "landed"
    first, _ = await db.a.admit(check(db.a, user), 0.30)
    _charge(db.a, first, 1.00)  # it cost the whole limit: $0.70 above its estimate
    db.a.record(user, 0.05)  # and a plain cost, under no reservation, in the same batch

    settling = Gate("before_commit")
    db.a.settle_pause = settling
    first.release()
    await asyncio.wait_for(settling.reached.wait(), 5)  # written, not committed

    reading = Gate("read")
    db.a.pause = reading
    second = asyncio.create_task(db.a.admit(check(db.a, user), 0.30))
    await asyncio.wait_for(reading.reached.wait(), 5)  # it has read: $0.30 held, nothing spent

    settling.go.set()  # the settlement commits, and its answer is handled first
    assert await db.a.flush(5)
    assert first.state == "retired"
    assert await _account(db.pool, user) == Decimal("1.05")

    reading.go.set()
    admitted, refusal = await second
    assert admitted is None and refusal is not None  # the limit is spent

    # Once nothing races, the counters alone say so, and nothing is counted twice:
    # a larger limit has exactly what is left of it.
    db.a.pause = db.a.settle_pause = None
    later, _ = await db.a.admit(check(db.a, user, daily=2.0), 5.0)
    assert later.amount == Decimal("0.950000")
    later.release()
    assert await db.a.flush(5)


# -- settlement receipts --------------------------------------------------------


async def _active(ledger, user: str, *, key_id=None, project_id=None, amount=0.30):
    reservation, _ = await ledger.admit(
        check(ledger, user, daily=10.0, key_id=key_id, project_id=project_id), amount
    )
    # Held by the test, not queued: settled through hand-made batches below.
    ledger._active.pop(reservation.reservation_id)
    return reservation


def _member(reservation, *costs: float, kind="settle", **override) -> Member:
    fields = {
        "reservation_id": reservation.reservation_id,
        "user_id": reservation.user_id,
        "api_key_id": reservation.key_id,
        "project_id": reservation.project_id,
        "kind": kind,
        "costs": tuple(
            CostLine(
                reservation.user_id,
                utc_day(),
                reservation.key_id,
                quantized_amount(c),
                1,
                reservation.project_id,
            )
            for c in costs
        ),
    }
    fields.update(override)
    return Member(**fields)


async def test_a_batch_applies_once_and_its_id_cannot_carry_another_payload(db):
    backend = db.a._backend
    u, v = PREFIX + "u", PREFIX + "v"
    r1 = await _active(db.a, u)
    r2 = await _active(db.a, u)
    plain = CostLine(v, utc_day(), None, quantized_amount(0.2))
    b1 = SettlementBatch.make("inst-a", [_member(r1, 0.5)], [plain])

    first = await backend.settle(b1)
    assert first.outcome == "applied" and first.outcome_of(r1.reservation_id) == "settled"
    assert (await _account(db.pool, u), await _account(db.pool, v)) == (
        Decimal("0.5"),
        Decimal("0.2"),
    )

    again = await backend.settle(b1)
    assert again.outcome == "replayed" and again.outcome_of(r1.reservation_id) == "settled"
    changed = SettlementBatch.make("inst-a", [_member(r1, 0.9)], [plain], batch_id=b1.batch_id)
    assert (await backend.settle(changed)).outcome == "conflict"

    # Overlap under another id: the member already retired is not charged again.
    b2 = SettlementBatch.make("inst-a", [_member(r1, 0.5), _member(r2, 0.1)], [])
    second = await backend.settle(b2)
    assert second.outcome_of(r1.reservation_id) == "duplicate"
    assert second.outcome_of(r2.reservation_id) == "settled"
    assert (await _account(db.pool, u), await _account(db.pool, v)) == (
        Decimal("0.6"),
        Decimal("0.2"),
    )
    assert await _members(db.pool) == {r1.reservation_id: "settled", r2.reservation_id: "settled"}


async def test_settlement_refuses_inconsistent_members_before_writing(db):
    backend = db.a._backend
    u = PREFIX + "own"
    r = await _active(db.a, u, key_id=71, project_id="p-ss-1")

    listed_twice = SettlementBatch.make("inst-a", [_member(r, 0.1), _member(r, 0.1)], [])
    elsewhere = SettlementBatch.make(
        "inst-a",
        [
            _member(
                r,
                costs=(
                    CostLine(PREFIX + "other", utc_day(), 71, quantized_amount(0.1), 1, "p-ss-1"),
                ),
            )
        ],
        [],
    )
    for batch in (listed_twice, elsewhere):
        assert (await backend.settle(batch)).outcome == "invalid"

    # Same id, presented for another account, key or project, or by another
    # instance: the row is not retired and nothing is charged.
    for override in (
        {"user_id": PREFIX + "other", "costs": ()},
        {"api_key_id": 72, "costs": ()},
        {"project_id": "p-ss-2", "costs": ()},
    ):
        batch = SettlementBatch.make("inst-a", [_member(r, **override)], [])
        assert (await backend.settle(batch)).outcome_of(r.reservation_id) == "mismatch"
    foreign = SettlementBatch.make("inst-b", [_member(r, 0.1)], [])
    assert (await db.b._backend.settle(foreign)).outcome_of(r.reservation_id) == "mismatch"

    assert [row["state"] for row in await _rows(db.pool, u)] == ["active"]
    assert await _account(db.pool, u) == 0
    async with db.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM spend_settlements WHERE batch_id = ANY($1::text[])",
                [listed_twice.batch_id, elsewhere.batch_id],
            )
            == 0
        )


async def test_an_admission_identity_cannot_be_reused_once_retired_or_for_another_request(db):
    from serving.storage.pools import request_connection

    backend = db.a._backend
    user = PREFIX + "ident"

    async def admit(request):
        async with request_connection(backend.request_pool) as conn:
            return await backend.admit_on(
                conn, request, lambda s: Decimal("0.3"), budget_ms=3000, on_xid=lambda x: None
            )

    base = AdmissionRequest(
        reservation_id="sr_ident_1",
        instance_id="inst-a",
        user_id=user,
        api_key_id=None,
        project_id=None,
        requested_amount=Decimal("0.300000"),
        check_day=utc_day(),
        operation="request",
    )
    assert (await admit(base)).result == "admitted"
    assert (await admit(base)).result == "existing"  # a retry after a lost answer
    import dataclasses

    for change in (
        {"user_id": PREFIX + "else"},
        {"requested_amount": Decimal("0.5")},
        {"operation": "batch"},
        {"check_day": "2000-01-01"},
    ):
        assert (await admit(dataclasses.replace(base, **change))).result == "conflict"

    member = Member(base.reservation_id, user, "settle")
    await backend.settle(SettlementBatch.make("inst-a", [member], []))
    assert await _rows(db.pool, user) == []
    # Retired: the same identity can never admit (and dispatch) again.
    assert (await admit(base)).result == "retired"

    # A quarantined reservation keeps its row: its identity is retired as well.
    held = dataclasses.replace(base, reservation_id="sr_ident_2")
    assert (await admit(held)).result == "admitted"
    quarantined = Member(held.reservation_id, user, "settle")
    marked = await backend.quarantine(quarantined, "inst-a", day=utc_day(), note="refused")
    assert marked == "quarantined"
    assert (await admit(held)).result == "retired"


# -- the writer: lost answers, cancellation, late and multiple costs -----------


class LosesFirstAnswer:
    """The real backend, except the first settlement's answer is lost after it committed."""

    def __init__(self, backend) -> None:
        self._backend = backend
        self.lost = 0

    def __getattr__(self, name):
        return getattr(self._backend, name)

    async def settle(self, batch, **kwargs):
        result = await self._backend.settle(batch, **kwargs)
        if not self.lost:
            self.lost += 1
            raise asyncpg.exceptions.ConnectionDoesNotExistError("answer lost")
        return result


async def test_a_settlement_whose_answer_was_lost_is_sent_again_and_counted_once(db):
    user = PREFIX + "lost"
    db.a._backend = LosesFirstAnswer(db.a._backend)
    reservation = await _admit_and_finish(db.a, user, estimate=0.30, cost=0.50)
    plain_user = PREFIX + "lost-plain"
    db.a.record(plain_user, 0.25)
    reservation.release()
    assert await db.a.flush(10)
    assert db.a._backend.lost == 1
    assert await _account(db.pool, user) == Decimal("0.5")
    assert await _account(db.pool, plain_user) == Decimal("0.25")
    assert await _rows(db.pool, user) == []
    assert db.a.fully_settled()


async def test_a_writer_cancelled_before_commit_writes_the_same_batch_once_later(db):
    user = PREFIX + "cancel-writer"
    reservation = await _admit_and_finish(db.a, user, estimate=0.30, cost=0.40)
    gate = Gate("before_commit")
    db.a.settle_pause = gate
    reservation.release()
    await asyncio.wait_for(gate.reached.wait(), 5)
    batch_id = db.a._writing.batch_id
    db.a._writer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await db.a._writer
    assert db.a.holdings()["queued"] == 1
    assert await _account(db.pool, user) == 0  # rolled back

    gate.go.set()
    assert await db.a.flush(5)
    assert await _account(db.pool, user) == Decimal("0.4")
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT batch_id FROM spend_settlements") == batch_id


async def test_several_costs_and_a_late_cost_are_all_charged_and_counted_meanwhile(db):
    user = PREFIX + "multi"
    reservation, _ = await db.a.admit(check(db.a, user, daily=1.0), 0.30)
    _charge(db.a, reservation, 0.20)
    _charge(db.a, reservation, 0.30)
    # Still running, it has cost $0.50 against $0.30 reserved: this process's
    # next admission sees the $0.20 above the estimate at once.
    assert db.a._local_extra(user, None, None, reserved=False)[0] == pytest.approx(0.20)
    nxt, _ = await db.a.admit(check(db.a, user, daily=1.0), 1.0)
    assert nxt.amount == Decimal("0.500000")  # $1 - $0.30 reserved - $0.20 known excess
    db.a._active.pop(nxt.reservation_id)

    reservation.release()
    _charge(db.a, reservation, 0.05)  # after the request ended: a plain increment
    assert await db.a.flush(5)
    assert await _account(db.pool, user) == Decimal("0.55")
    assert [row["reservation_id"] for row in await _rows(db.pool, user)] == [nxt.reservation_id]


async def test_an_admission_cancelled_before_its_commit_leaves_nothing(db):
    user = PREFIX + "cancel-before"
    gate = Gate("inserted")
    db.a.pause = gate
    task = asyncio.create_task(db.a.admit(check(db.a, user), 0.30))
    await asyncio.wait_for(gate.reached.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.a.holdings()["abandoned"] == 1
    db.a.pause = None
    assert await db.a.flush(5)
    assert await _rows(db.pool, user) == []
    assert db.a.fully_settled()


async def test_an_admission_cancelled_after_its_commit_is_released_never_charged(db):
    user = PREFIX + "cancel-after"
    gate = Gate("committed")
    db.a.pause = gate
    task = asyncio.create_task(db.a.admit(check(db.a, user), 0.30))
    await asyncio.wait_for(gate.reached.wait(), 5)
    assert len(await _rows(db.pool, user)) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    db.a.pause = None
    assert await db.a.flush(5)
    assert await _rows(db.pool, user) == []
    assert list((await _members(db.pool)).values()) == ["released"]
    assert await _account(db.pool, user) == 0
    assert db.a.fully_settled()


# -- refusals, liabilities, crashes ---------------------------------------------


async def test_a_refused_entry_is_split_out_and_kept_counted(db):
    full, fine = PREFIX + "full", PREFIX + "fine"
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests) "
            "VALUES ($1, $2, 999999.9, 1)",
            full,
            utc_day(),
        )
    overflowing, _ = await db.a.admit(check(db.a, full, daily=2_000_000.0), 0.30)
    ok = await _admit_and_finish(db.a, fine, estimate=0.30, cost=0.10)
    _charge(db.a, overflowing, 0.5)
    overflowing.release()
    ok.release()
    assert await db.a.flush(10)

    assert await _account(db.pool, fine) == Decimal("0.1")
    rows = await _rows(db.pool, full)
    assert [(r["state"], r["cost_amount"]) for r in rows] == [("quarantined", Decimal("0.5"))]
    assert rows[0]["liability_day"] == utc_day()
    assert db.a.holdings()["quarantined"] == 1
    assert db.a.unwritten() == (pytest.approx(0.5), 1)
    assert not db.a.fully_settled()


class MarksLate(RefusesCosts):
    """The real backend, refusing costs, whose quarantine mark is held up, fails, or loses its answer."""

    def __init__(self, backend, how: str) -> None:
        super().__init__(backend)
        self.how = how
        self.reached = asyncio.Event()
        self.go = asyncio.Event()
        self.calls = 0

    async def quarantine(self, member, instance_id, **kwargs):
        self.calls += 1
        if self.how == "held":
            self.reached.set()
            await self.go.wait()
        elif self.how == "fails" and self.calls == 1:
            raise ConnectionResetError("lost before the mark was written")
        elif self.how == "answer lost" and self.calls == 1:
            await self._backend.quarantine(member, instance_id, **kwargs)
            raise ConnectionResetError("the mark was written, its answer lost")
        return await self._backend.quarantine(member, instance_id, **kwargs)


async def test_a_refused_request_stays_counted_while_its_row_is_being_marked(db):
    """The counters refused a request's cost; its row is being marked quarantined.

    Until the mark commits the row is active at its estimate, so the part of
    the cost above the estimate is known to this process alone -- which must
    go on counting it, not take the row to hold it already. Counted as
    quarantined at once, a request that cost the whole $1.00 limit let the
    next one in while its mark was under way.
    """
    user = PREFIX + "marking"
    backend = MarksLate(db.a._backend, "held")
    ledger = SharedSpendLedger(backend, instance_id="inst-marking")
    first, _ = await ledger.admit(check(ledger, user), 0.30)
    _charge(ledger, first, 1.00)
    first.release()
    flushing = asyncio.create_task(ledger.flush(10))
    await asyncio.wait_for(backend.reached.wait(), 5)

    assert [(r["state"], r["amount"]) for r in await _rows(db.pool, user)] == [
        ("active", Decimal("0.3"))
    ]
    second, refusal = await ledger.admit(check(ledger, user), 0.30)
    assert second is None and refusal is not None  # the limit is spent

    backend.go.set()
    assert await flushing
    assert first.state == "quarantined"
    third, refusal = await ledger.admit(check(ledger, user), 0.30)
    assert third is None and refusal is not None  # and stays spent, on its row now
    # Nothing is counted twice once the mark is in: a larger limit has what is left.
    later, _ = await db.b.admit(check(db.b, user, daily=2.0), 5.0)
    assert later.amount == Decimal("1.000000")
    later.release()
    assert await db.b.flush(5)


@pytest.mark.parametrize("how", ["fails", "answer lost"])
async def test_a_mark_that_got_no_answer_is_sent_again_and_counts_nothing_twice(
    db, monkeypatch, how
):
    # Whether the mark was written before its answer was lost or not written
    # at all, the process cannot tell: it sends it again (the mark is harmless
    # to repeat) instead of guessing. Guessing "not marked" and counting the
    # cost itself held it twice for the rest of the day.
    from serving.shared_spend import ledger as shared_ledger

    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 0.01)
    user = PREFIX + "mark-" + how.replace(" ", "-")
    backend = MarksLate(db.a._backend, how)
    ledger = SharedSpendLedger(backend, instance_id="inst-marking")
    first, _ = await ledger.admit(check(ledger, user), 0.30)
    _charge(ledger, first, 0.50)  # $0.50 of the $1.00 limit
    first.release()
    assert await ledger.flush(10)

    assert backend.calls == 2 and first.state == "quarantined"
    assert [(r["state"], r["cost_amount"]) for r in await _rows(db.pool, user)] == [
        ("quarantined", Decimal("0.5"))
    ]
    assert ledger.holdings()["quarantined"] == 1 and ledger.holdings()["unaccounted"] == 0
    for asking in (ledger, db.b):  # the process that holds it, and another
        later, _ = await asking.admit(check(asking, user), 5.0)
        assert later.amount == Decimal("0.500000")  # exactly what is left
        later.release()
        assert await asking.flush(5)
    if ledger._retry is not None:
        ledger._retry.cancel()


async def test_a_mark_that_waited_for_a_reconciliation_learns_the_row_was_retired(db):
    """A mark and an operator's reconciliation of the same reservation, at the same moment.

    The reconciliation deletes the row and records its receipt in one commit.
    A mark sent meanwhile waits for it on the row, then finds no row -- and
    has to see the receipt that commit left, which its own statement's
    snapshot predates. Read in the same statement, the receipt was not there:
    the reservation was called missing, and its process went on counting, for
    the rest of the day, a cost the reconciliation had already charged.
    """
    user = PREFIX + "mark-race"
    reservation, _ = await db.a.admit(check(db.a, user), 0.30)
    member = Member(reservation.reservation_id, user, "settle")

    other = await db.pool.acquire()
    try:
        retiring_pid = await other.fetchval("SELECT pg_backend_pid()")
        retiring = other.transaction()
        await retiring.start()
        # What a reconciliation's settlement does for this member, left open.
        await other.execute(
            "DELETE FROM spend_reservations WHERE reservation_id = $1", member.reservation_id
        )
        await other.execute(
            "INSERT INTO spend_settlement_members (reservation_id, batch_id, outcome) "
            "VALUES ($1, 'op_race', 'settled')",
            member.reservation_id,
        )
        marking = asyncio.create_task(
            db.a._backend.quarantine(member, "inst-a", day=utc_day(), note="refused")
        )

        async def waiting_on_the_row() -> bool:
            # The mark's own session, held up by this transaction: any other
            # wait on the server (the suite's other databases share it) would
            # let the commit through before the mark had started, and a mark
            # that starts after the commit sees the receipt whatever it reads.
            async with db.pool.acquire() as conn:
                return bool(
                    await conn.fetchval(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE $1 = ANY(pg_blocking_pids(pid))",
                        retiring_pid,
                    )
                )

        await _until(waiting_on_the_row)
        await retiring.commit()
    finally:
        await db.pool.release(other)
    assert await marking == "retired"
    db.a._active.pop(reservation.reservation_id)  # retired by the "operator": nothing to settle


async def test_a_mark_for_a_hard_deleted_account_says_so(db):
    user = PREFIX + "mark-erased"
    reservation, _ = await db.a.admit(check(db.a, user), 0.30)
    member = Member(reservation.reservation_id, user, "settle")
    digest = fence_account_digest(user, postgres.FENCE_SECRET)
    db.erased.append(digest)
    async with db.pool.acquire() as conn:
        # As a hard delete leaves it: the tombstone, and the account's rows gone.
        await conn.execute("INSERT INTO erasure_fence (account_digest) VALUES ($1)", digest)
        await conn.execute("DELETE FROM spend_reservations WHERE user_id = $1", user)
    backend = db.a._backend
    assert await backend.quarantine(member, "inst-a", day=utc_day(), note="refused") == "erased"
    # A reservation nothing accounts for is still told apart.
    stray = Member("sr_never_admitted", PREFIX + "mark-stray", "settle")
    assert await backend.quarantine(stray, "inst-a", day=utc_day(), note="refused") == "missing"
    db.a._active.pop(reservation.reservation_id)


async def test_a_request_that_ended_holds_nothing_whatever_it_was_charged(db):
    """What a request is charged is what its billing point recorded; the rest is given back.

    Four requests of $0.30 against $1.00 that end with nothing charged (the
    upstream failed them, say) leave nothing held: the fifth is admitted in
    full by either process, and the gate of the process that served them
    passes it as the other's does. Kept as liabilities they would have used up
    the quota with nothing spent.
    """
    user = PREFIX + "ended"
    for _ in range(4):
        reservation, refusal = await db.a.admit(check(db.a, user), 0.30)
        assert refusal is None
        reservation.release()
        assert await db.a.flush(5)
    assert await _rows(db.pool, user) == []
    assert await _account(db.pool, user) == 0
    assert db.a.fully_settled()
    for ledger in (db.a, db.b):
        assert ledger.refusal(check(ledger, user)) is None
        admitted, refusal = await ledger.admit(check(ledger, user), 0.30)
        assert refusal is None and admitted.amount == Decimal("0.300000")
        admitted.release()
        assert await ledger.flush(5)

    # One that was charged for part of what it reserved: exactly that is added.
    partly, _ = await db.a.admit(check(db.a, user), 0.30)
    _charge(db.a, partly, 0.10)
    partly.release()
    assert await db.a.flush(5)
    assert await _account(db.pool, user) == Decimal("0.1")
    assert await _rows(db.pool, user) == []
    snapshot = await _snapshot(db.b, user)
    assert (snapshot.account_day, snapshot.reserved_account_day) == (Decimal("0.1"), Decimal(0))
    assert sorted((await _members(db.pool)).values()) == [*["released"] * 6, "settled"]


async def test_a_crashed_process_leaves_its_reservations_counted_and_listed(db):
    from serving.shared_spend.__main__ import run

    user = PREFIX + "crash"
    crashed = SharedSpendLedger(db.a._backend, instance_id="inst-crashed")
    stranded, _ = await crashed.admit(check(crashed, user), 0.30)
    _charge(crashed, stranded, 0.80)  # known only to the process that crashes
    del crashed, stranded  # gone: nothing settles it, nothing expires it

    later, _ = await db.b.admit(check(db.b, user), 1.0)
    assert later.amount == Decimal("0.700000")
    status = json.loads((await run(SimpleNamespace(command="status"), db.b._backend))[0])
    states = {(r["instance_id"], r["state"]): r["n"] for r in status["open_reservations"]}
    assert states == {("inst-crashed", "active"): 1, ("inst-b", "active"): 1}
    assert status["mode"] == "local" and status["participation_proven"] is False
    # Neither test ledger registers: both owners are "not heard from", so both
    # reservations are what an operator is pointed at ($0.30 + $0.70).
    assert {r["owner"] for r in status["open_reservations"]} == {"not registered"}
    assert status["stranded"]["reservations"] == 2
    assert Decimal(str(status["stranded"]["amount"])) == Decimal("1.0")
    assert status["stranded"]["instances"] == ["inst-b", "inst-crashed"]
    assert status["stranded"]["oldest_age_seconds"] >= 0
    listing = await run(SimpleNamespace(command="list", limit=10), db.b._backend)
    assert json.loads(listing[0])["owner"] == "not registered"
    assert user not in "".join(listing)  # no account names in the listing


async def test_a_reservation_no_longer_held_stays_counted_and_reported(db):
    user = PREFIX + "missing"
    reservation = await _admit_and_finish(db.a, user, estimate=0.30, cost=0.40)
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM spend_reservations WHERE reservation_id = $1",
            reservation.reservation_id,
        )
    reservation.release()
    assert await db.a.flush(5)
    assert await _account(db.pool, user) == 0  # not charged blindly
    assert db.a.holdings()["unaccounted"] == 1
    assert db.a.unwritten() == (pytest.approx(0.4), 1)
    assert db.a._local_extra(user, None, None, reserved=False)[0] == pytest.approx(0.4)
    assert not db.a.fully_settled()


async def test_an_erased_account_is_not_admitted_and_its_settlement_writes_nothing(db):
    user = PREFIX + "erased"
    reservation = await _admit_and_finish(db.a, user, estimate=0.30, cost=0.20)
    digest = fence_account_digest(user, postgres.FENCE_SECRET)
    db.erased.append(digest)
    async with db.pool.acquire() as conn:
        await conn.execute("INSERT INTO erasure_fence (account_digest) VALUES ($1)", digest)
        await conn.execute("DELETE FROM spend_reservations WHERE user_id = $1", user)

    with pytest.raises(AccountErased):
        await db.b.admit(check(db.b, user), 0.30)
    reservation.release()
    assert await db.a.flush(5)
    assert await _account(db.pool, user) == 0
    assert await _rows(db.pool, user) == []
    assert db.a.holdings()["unaccounted"] == 0


async def test_reusing_a_retired_identity_is_refused_by_the_ledger(db, monkeypatch):
    user = PREFIX + "reuse"
    reservation, _ = await db.a.admit(check(db.a, user), 0.30)
    reservation.release()
    assert await db.a.flush(5)
    same = SimpleNamespace(hex=reservation.reservation_id.removeprefix("sr_"))
    monkeypatch.setattr("serving.shared_spend.ledger.uuid.uuid4", lambda: same)
    with pytest.raises(ReservationConflict):
        await db.a.admit(check(db.a, user), 0.30)


# -- mode and evidence ----------------------------------------------------------


async def test_the_mode_is_local_by_default_and_read_once(db):
    assert await spend_mode.load(db.pool) == "local"
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE spend_accounting SET mode = 'shared' WHERE id")
    try:
        assert await spend_mode.load(db.pool) == "shared"
        assert spend_mode.capability() == {
            "protocol": 2,
            "mode": "shared",
            "currency": "USD",
            "native_amounts": 1,
            "usd_to_currency_rate": "1",
        }
    finally:
        spend_mode.set_mode("local")


async def test_counter_writes_outside_the_shared_settlement_are_recorded_in_shared_mode(db):
    async def evidence() -> int:
        async with db.pool.acquire() as conn:
            return await conn.fetchval("SELECT count(*) FROM spend_unidentified_writes")

    await db.store.increment_user_cost(PREFIX + "legacy", 0.1, day=utc_day())
    assert await evidence() == 0  # local mode: nothing recorded
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE spend_accounting SET mode = 'shared' WHERE id")
    await db.store.increment_user_cost(PREFIX + "legacy", 0.1, day=utc_day())
    legacy = await evidence()
    assert legacy >= 1
    assert await _account(db.pool, PREFIX + "legacy") == Decimal("0.2")  # never refused
    reservation = await _admit_and_finish(db.a, PREFIX + "shared", estimate=0.3, cost=0.1)
    reservation.release()
    assert await db.a.flush(5)
    assert await evidence() == legacy  # the shared settlement is its own


# -- two real processes ----------------------------------------------------------

_PROCESS_SCRIPT = r"""
import asyncio, json, sys, time
from serving.spend_ledger import SpendCheck
from serving.shared_spend.ledger import SharedSpendLedger
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

async def main(instance, user, start_at):
    pool = await postgres.create_pool(min_size=1, max_size=4)
    ledger = SharedSpendLedger(
        PostgresOperationalStore(pool, fence_secret=postgres.FENCE_SECRET).shared_spend_backend(),
        instance_id=instance,
    )
    while time.time() < start_at:
        await asyncio.sleep(0.005)
    def check():
        return SpendCheck(ledger=ledger, user_id=user, key_id=None, read_at=time.monotonic(),
                          account_quota_usd=1.0, account_spent_usd=0.0)
    results = await asyncio.gather(*[ledger.admit(check(), 0.30) for _ in range(10)])
    print(json.dumps([str(r.amount) for r, _ in results if r is not None]))
    await pool.close()

asyncio.run(main(sys.argv[1], sys.argv[2], float(sys.argv[3])))
"""


async def test_two_os_processes_share_one_accounts_headroom(db):
    user = PREFIX + "procs"
    start_at = time.time() + 2.0
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    procs = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _PROCESS_SCRIPT,
            name,
            user,
            str(start_at),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        for name in ("proc-a", "proc-b")
    ]
    outputs = [await asyncio.wait_for(p.communicate(), 60) for p in procs]
    for proc, (_, err) in zip(procs, outputs, strict=True):
        assert proc.returncode == 0, err.decode()[-2000:]
    amounts = sorted(Decimal(x) for out, _ in outputs for x in json.loads(out))
    assert amounts == [Decimal("0.100000"), *[Decimal("0.300000")] * 3]
    rows = await _rows(db.pool, user)
    assert sum(r["amount"] for r in rows) == Decimal("1.000000")


# -- the production wrapper: one ledger from the gate to the billing point ------


async def test_the_production_store_wrapper_carries_admission_and_charge_to_one_ledger(
    db, monkeypatch
):
    from serving.servers.routers.completions_cost import CostTracker, PricingLookup
    from serving.servers.routers.routing_info import Pricing, RoutingInfo
    from serving.spend_ledger import ledger_for
    from serving.storage.cache import CachedOperationalStore, InMemoryCache

    monkeypatch.setattr(spend_mode, "_mode", "shared")
    wrapped = CachedOperationalStore(db.store, InMemoryCache())
    ledger = ledger_for(wrapped)
    assert ledger is ledger_for(db.store)
    user = PREFIX + "wrapped"
    reservation, _ = await check(ledger, user).reserve(0.30)

    tracker = CostTracker(op_store=wrapped, pricing=PricingLookup(router=None))
    routing = RoutingInfo(
        request_id="r-1", model="m", pricing=Pricing(prompt_price=1.0, completion_price=2.0)
    )
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        tracker.charge(user_id=user, routing=routing, prompt_tokens=1000, completion_tokens=1000)
    assert reservation.cost() == Decimal("0.003")
    reservation.release()
    assert await ledger.flush(5)
    assert await _account(db.pool, user) == Decimal("0.003")
    assert list((await _members(db.pool)).values()) == ["settled"]
