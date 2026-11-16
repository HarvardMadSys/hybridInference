"""The shared ledger's own logic, on an in-memory backend: locks, meters, liabilities."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import MagicMock

import asyncpg
import pytest

from serving.shared_spend import ledger as shared_ledger, mode as spend_mode
from serving.shared_spend.ledger import SharedSpendLedger
from serving.shared_spend.store import CostLine, Member, SettlementBatch, usd
from serving.spend_ledger import SpendCheck, SpendLedger, ledger_for, utc_day
from serving.utils import context as req_ctx
from tests.fixtures.shared_spend import FakeSharedBackend

pytestmark = pytest.mark.asyncio

USER = "u-unit"


def _ledger() -> tuple[SharedSpendLedger, FakeSharedBackend]:
    backend = FakeSharedBackend()
    return SharedSpendLedger(backend, instance_id="inst-unit"), backend


def _check(ledger, *, quota: float = 1.0) -> SpendCheck:
    return SpendCheck(
        ledger=ledger,
        user_id=USER,
        key_id=None,
        read_at=time.monotonic(),
        account_quota_usd=quota,
        account_spent_usd=0.0,
    )


async def test_a_waiter_cancelled_while_waiting_never_releases_the_holders_lock():
    ledger, _ = _ledger()
    key = (1, 1)
    holder_in = asyncio.Event()
    holder_go = asyncio.Event()

    async def holder():
        async with ledger._local_locks([key]):
            holder_in.set()
            await holder_go.wait()

    held = asyncio.create_task(holder())
    await holder_in.wait()

    async def waiter():
        async with ledger._local_locks([key]):
            pass

    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert ledger._locks[key][1] == 2
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    lock = ledger._locks[key][0]
    assert lock.locked()  # still the holder's
    assert ledger._locks[key][1] == 1

    third_in = asyncio.Event()

    async def third():
        async with ledger._local_locks([key]):
            third_in.set()

    later = asyncio.create_task(third())
    await asyncio.sleep(0.01)
    assert not third_in.is_set()  # it waits for the holder, not for nobody
    holder_go.set()
    await held
    await later
    assert third_in.is_set()
    assert ledger._locks == {}


async def test_cancelled_after_its_first_lock_it_releases_that_one_only():
    ledger, _ = _ledger()
    first, second = (1, 1), (1, 2)
    other_in = asyncio.Event()
    other_go = asyncio.Event()

    async def holds_second():
        async with ledger._local_locks([second]):
            other_in.set()
            await other_go.wait()

    other = asyncio.create_task(holds_second())
    await other_in.wait()

    async def both():
        async with ledger._local_locks([first, second]):
            pass

    task = asyncio.create_task(both())
    await asyncio.sleep(0.01)
    assert ledger._locks[first][0].locked()  # took the first, waits for the second
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first not in ledger._locks  # given back and forgotten
    assert ledger._locks[second][0].locked()  # the other admission's, untouched
    other_go.set()
    await other
    assert ledger._locks == {}


async def test_a_batch_being_written_is_counted_once_in_the_meters():
    ledger, backend = _ledger()
    backend.settle_gate = asyncio.Event()
    ledger.record(USER, 0.25)
    await asyncio.wait_for(backend.settling.wait(), 5)
    assert ledger._writing is not None and ledger._writing in ledger._replay
    assert ledger._local_extra(USER, None, None, reserved=False)[0] == pytest.approx(0.25)
    assert ledger.unwritten() == (pytest.approx(0.25), 1)

    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    reservation.release()
    assert ledger.holdings()["queued"] == 1
    backend.settle_gate.set()
    assert await ledger.flush(5)
    assert ledger.fully_settled()
    assert backend.counters[USER] == Decimal("0.25")


async def test_a_running_request_that_cost_more_than_its_estimate_holds_the_difference():
    from serving.utils import context as req_ctx

    ledger, _backend = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(USER, 0.20)
        ledger.record(USER, 0.25)
    assert reservation.cost() == Decimal("0.45")
    assert ledger._local_extra(USER, None, None, reserved=False)[0] == pytest.approx(0.15)
    # The gate's early check, which reads only the counters, holds all $0.45.
    assert ledger._local_extra(USER, None, None, reserved=True)[0] == pytest.approx(0.45)
    second, _ = await ledger.admit(_check(ledger), 1.0)
    assert second.amount == Decimal("0.550000")  # $1 - $0.30 held - $0.15 above it


async def test_a_cost_attributed_elsewhere_is_not_attached_to_the_reservation():
    from serving.utils import context as req_ctx

    ledger, _ = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(USER, 0.10, api_key_id=99)  # another key
        ledger.record("someone-else", 0.10)
    assert reservation.charges == []
    assert len(ledger._increments) == 2


async def test_a_request_that_ended_holds_only_what_its_billing_point_recorded():
    # However it ended: released with no cost, the whole estimate comes back;
    # with a cost, exactly that is added. Four requests of $0.30 against $1.00
    # that were charged nothing leave the fifth its full $0.30.
    ledger, backend = _ledger()
    for _ in range(4):
        reservation, refusal = await ledger.admit(_check(ledger), 0.30)
        assert refusal is None
        reservation.release()
        reservation.release()  # idempotent
        assert await ledger.flush(5)
    assert backend.rows == {} and backend.counters == {}
    assert ledger.fully_settled()
    assert ledger.refusal(_check(ledger)) is None
    fifth, refusal = await ledger.admit(_check(ledger), 0.30)
    assert refusal is None and fifth.amount == Decimal("0.300000")
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: fifth}):
        ledger.record(USER, 0.12)
    fifth.release()
    assert await ledger.flush(5)
    assert backend.rows == {} and backend.counters == {USER: Decimal("0.12")}


async def test_a_liability_counts_against_the_day_it_ended_on_and_no_later_one(monkeypatch):
    # The gate's early check adds what this process holds to what it read. A
    # reservation the counters refused (quarantined) is held on the day it
    # ended, as its row is in the database: counted against a later day, this
    # process's gate alone would go on refusing a caller every other gate, and
    # the admission itself, lets through.
    day = {"today": "2031-03-15"}
    monkeypatch.setattr("serving.spend_ledger.utc_day", lambda now=None: day["today"])
    ledger, backend = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(USER, 0.50)
    backend.fail_settle = asyncpg.exceptions.NumericValueOutOfRangeError("numeric field overflow")
    reservation.release()
    assert await ledger.flush(5)
    backend.fail_settle = None
    assert (reservation.state, reservation.ended_day) == ("quarantined", "2031-03-15")

    def gate() -> tuple[float, float]:
        extra = ledger._local_extra(USER, None, None, reserved=True)
        return (pytest.approx(extra[0]), pytest.approx(extra[1]))

    assert gate() == (0.5, 0.5)  # its day: the larger of its estimate and its cost
    assert ledger.refusal(_check(ledger, quota=0.5)) is not None
    day["today"] = "2031-03-16"
    assert gate() == (0.0, 0.5)  # the next day: its month only
    assert ledger.refusal(_check(ledger, quota=0.5)) is None
    day["today"] = "2031-04-01"
    assert gate() == (0.0, 0.0)  # the next month: nothing
    # Set apart from what every check reads, never from what is reported.
    assert ledger._liabilities == {} and len(ledger._past_liabilities) == 1
    assert ledger.holdings()["quarantined"] == 1
    assert ledger.unwritten() == (pytest.approx(0.5), 1)
    assert not ledger.fully_settled()


async def test_a_defect_in_the_writer_is_retried_after_a_back_off_not_at_once_and_not_never(
    monkeypatch,
):
    # Not a failed write (those are answered, and the batch sent again):
    # something raising in the writer's own bookkeeping. Tried again at once it
    # would spin for as long as the defect lasts; never tried again, what is
    # queued would wait for the next request to wake the writer.
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 0.05)
    ledger, backend = _ledger()
    next_batch = ledger._next_batch
    attempts: list[float] = []
    defect = {"present": True}
    loop = asyncio.get_running_loop()

    def defective():
        attempts.append(loop.time())
        if defect["present"]:
            raise RuntimeError("a defect before anything was sent")
        return next_batch()

    ledger._next_batch = defective
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    reservation.release()
    await asyncio.sleep(0.2)
    # At 0, 0.05 and 0.15 s: a few attempts, backing off -- not thousands.
    assert 2 <= len(attempts) <= 5
    assert len(backend.rows) == 1  # nothing was written, nothing was lost

    defect["present"] = False
    deadline = loop.time() + 5
    while backend.rows and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert backend.rows == {}
    assert ledger.fully_settled()


async def test_a_defect_while_building_a_batch_loses_nothing_it_had_taken(monkeypatch):
    # The writer takes what is queued to build a batch. Should building it fail
    # (a defect, the case the back-off above exists for), the next attempt has
    # to find the same reservations and costs: dropped, a plain cost would be
    # in no counter and counted nowhere, and a reservation would stay held in
    # the database with nothing left to settle it.
    import types

    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 0.01)
    ledger, backend = _ledger()
    built: list[int] = []

    def defective_once(*args, **kwargs):
        built.append(len(built))
        if len(built) == 1:
            raise RuntimeError("a defect while building the batch")
        return SettlementBatch.make(*args, **kwargs)

    monkeypatch.setattr(
        shared_ledger, "SettlementBatch", types.SimpleNamespace(make=defective_once)
    )
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(USER, 0.12)
    ledger.record(USER, 0.05)  # a plain cost, under no reservation
    reservation.release()

    assert await ledger.flush(5)
    assert len(built) >= 2  # the first attempt failed, a later one went out
    assert backend.rows == {}
    assert backend.counters == {USER: Decimal("0.17")}
    assert ledger.fully_settled()
    (batch,) = backend.batches
    assert [m.reservation_id for m in batch.members] == [reservation.reservation_id]
    assert sum(line.cost_usd for line in batch.increments) == Decimal("0.05")


async def test_an_admission_whose_connection_failed_is_tried_again_and_admitted():
    # What a lost COMMIT answer, a failover or a reaped idle connection looks
    # like: asyncpg's connection errors carry a class 08 SQLSTATE. The same
    # admission is sent again within its budget instead of answering 503.
    ledger, backend = _ledger()
    backend.fail_admits = [
        asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed"),
        asyncpg.exceptions.ConnectionFailureError("connection failure"),
    ]
    reservation, refusal = await ledger.admit(_check(ledger), 0.30)
    assert refusal is None and reservation.amount == Decimal("0.300000")
    assert backend.fail_admits == [] and len(backend.rows) == 1
    assert ledger.stats()["admissions"] == {"admitted": 1}


@pytest.mark.parametrize(
    ("error", "again"),
    [
        (asyncpg.exceptions.ConnectionDoesNotExistError("closed"), True),
        (asyncpg.exceptions.ConnectionFailureError("failed"), True),
        (asyncpg.exceptions.UniqueViolationError("the id's row appeared"), True),
        (asyncpg.exceptions.SerializationError("serialization"), True),
        (asyncpg.exceptions.InterfaceError("connection is closed"), True),
        (ConnectionResetError("reset by peer"), True),
        # Final answers: the budget ran out, or the database said no.
        (asyncpg.exceptions.QueryCanceledError("statement timeout"), False),
        (asyncpg.exceptions.LockNotAvailableError("lock timeout"), False),
        (asyncpg.exceptions.IdleInTransactionSessionTimeoutError("idle"), False),
        (asyncpg.exceptions.NumericValueOutOfRangeError("overflow"), False),
        (TimeoutError(), False),
    ],
)
async def test_which_admission_failures_are_tried_again_with_the_same_id(error, again):
    assert shared_ledger._retryable(error) is again


async def test_what_a_settlement_moved_into_the_counters_stays_known_for_a_moment(monkeypatch):
    # For an admission whose read predates that settlement's commit: see the
    # PostgreSQL test of the race. Here, what is kept and for how long.
    ledger, backend = _ledger()
    before = time.monotonic()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(USER, 1.00)  # $0.70 above its estimate
    ledger.record(USER, 0.05)  # a plain cost in the same batch
    reservation.release()
    assert await ledger.flush(5)
    assert backend.counters == {USER: Decimal("1.05")}

    def extra(read_from):
        return ledger._local_extra(USER, None, None, reserved=False, read_from=read_from)[0]

    assert extra(None) == 0  # nothing is held here any more
    assert extra(before) == pytest.approx(0.75)  # a read made before the answer needs both
    assert extra(time.monotonic() + 1) == 0  # one made after it has them in the counters

    # Kept only for as long as an admission can run.
    monkeypatch.setattr(shared_ledger, "_LANDED_SECONDS", 0.0)
    await asyncio.sleep(0.01)
    ledger.record(USER, 0.01)
    assert await ledger.flush(5)
    assert len(ledger._landed) == 1 and extra(before) == pytest.approx(0.01)


async def test_a_refused_reservation_stays_a_waiting_one_until_its_mark_is_answered(monkeypatch):
    # The counters refused its cost, so its row is to be marked quarantined.
    # Until that mark is answered the row is active at its estimate, and what
    # the process holds must not assume otherwise: it goes on counting the part
    # of the cost above the estimate, as while the reservation waited, and it
    # sends the mark again until it gets an answer.
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 0.01)
    ledger, backend = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(USER, 0.50)
    backend.fail_settle = asyncpg.exceptions.NumericValueOutOfRangeError("numeric field overflow")
    backend.fail_quarantine = ConnectionRefusedError("the database went away")
    reservation.release()
    deadline = time.monotonic() + 5
    while backend.quarantine_calls < 2:  # sent again after it failed
        assert time.monotonic() < deadline
        await asyncio.sleep(0.005)
    assert not await ledger.flush(0.05)  # and still owed

    assert reservation.state == "queued"
    assert ledger.holdings()["queued"] == 1 and ledger.holdings()["quarantined"] == 0
    assert ledger._local_extra(USER, None, None, reserved=False)[0] == pytest.approx(0.20)
    assert ledger._local_extra(USER, None, None, reserved=True)[0] == pytest.approx(0.50)
    assert ledger.unwritten() == (pytest.approx(0.50), 1)
    assert await ledger.close() is False  # not everything is sent

    backend.fail_quarantine = None
    ledger._closed = False
    assert await ledger.flush(5)
    assert reservation.state == "quarantined"
    assert ledger.holdings()["quarantined"] == 1 and ledger.holdings()["queued"] == 0
    # Its row holds it now; an admission that read the row still active gets
    # the difference for a moment more.
    assert ledger._local_extra(USER, None, None, reserved=False)[0] == 0
    assert ledger._local_extra(USER, None, None, reserved=False, read_from=0.0)[0] == pytest.approx(
        0.20
    )


@pytest.mark.parametrize(
    ("answer", "state", "held", "duplicates"),
    [
        # Another batch retired it first: its cost goes nowhere, and that is said.
        ("retired", "retired", 0.0, 1),
        # The account was hard-deleted meanwhile: dropped, as a settlement drops it.
        ("erased", "retired", 0.0, 0),
        # Neither a row nor a receipt: nothing in the database counts it.
        ("missing", "unaccounted", 0.50, 0),
    ],
)
async def test_a_refused_reservation_no_longer_held_is_dropped_or_counted_here(
    answer, state, held, duplicates
):
    ledger, backend = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(USER, 0.50)
    backend.fail_settle = asyncpg.exceptions.NumericValueOutOfRangeError("numeric field overflow")
    backend.quarantine_answer = answer
    reservation.release()
    assert await ledger.flush(5)
    assert reservation.state == state
    assert ledger._local_extra(USER, None, None, reserved=False)[0] == pytest.approx(held)
    assert ledger.stats()["settlement"]["members_duplicate"] == duplicates


async def _refused(ledger, backend, user: str, cost: float):
    """A request of *user* whose settlement the counters refuse; its mark is then owed."""
    reservation, _ = await ledger.admit(
        SpendCheck(
            ledger=ledger,
            user_id=user,
            key_id=None,
            read_at=time.monotonic(),
            account_quota_usd=1000.0,
            account_spent_usd=0.0,
        ),
        0.30,
    )
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(user, cost)
    backend.fail_settle = asyncpg.exceptions.NumericValueOutOfRangeError("numeric field overflow")
    reservation.release()
    async with asyncio.timeout(5):
        while reservation.reservation_id not in {m.reservation_id for m, _, _ in ledger._marking}:
            if reservation.state != "queued":
                break
            await asyncio.sleep(0.005)
    backend.fail_settle = None
    return reservation


async def _idle(ledger) -> None:
    """Wait until the settlement writer has nothing more to do."""
    async with asyncio.timeout(2):
        while ledger._writer is not None:
            await asyncio.sleep(0.005)


async def test_a_mark_that_keeps_failing_does_not_pace_the_settlements(monkeypatch, caplog):
    # Marks have a back-off of their own. Sharing the writer's, one mark that
    # kept failing let a single batch through per back-off: requests that had
    # ended stayed held, more of them the busier the process was. Here that
    # would be ten seconds a batch, five batches.
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 10.0)
    ledger, backend = _ledger()
    backend.fail_quarantine = asyncpg.exceptions.InsufficientPrivilegeError("no UPDATE here")
    stuck = await _refused(ledger, backend, "u-stuck", 0.50)
    assert ledger._marking and stuck.state == "queued"

    ended = []
    for index in range(1200):  # several batches' worth
        reservation, _ = await ledger.admit(
            SpendCheck(
                ledger=ledger,
                user_id=f"u-busy-{index % 40}",
                key_id=None,
                read_at=time.monotonic(),
                account_quota_usd=1000.0,
                account_spent_usd=0.0,
            ),
            0.01,
        )
        ended.append(reservation)
    started = time.monotonic()
    for reservation in ended:
        reservation.release()
    while not all(r.state == "retired" for r in ended):
        assert time.monotonic() - started < 5.0, "the settlements are waiting for the mark"
        await asyncio.sleep(0.005)
    # And the mark was not tried again meanwhile.
    assert backend.quarantine_calls == 1
    assert stuck.state == "queued" and ledger.holdings()["queued"] == 1

    # Failing for a minute, it is reported once, at ERROR, with what is held up.
    ledger._mark_failing_since -= shared_ledger.SETTLEMENT_STALLED_SECONDS
    for _ in range(2):
        ledger._mark_after = 0.0
        await ledger._mark_refused()
    reported = [
        r for r in caplog.records if getattr(r, "event", None) == "spend_quarantine_mark_failing"
    ]
    assert len(reported) == 1 and reported[0].reservations == [stuck.reservation_id]

    backend.fail_quarantine = None
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 0.01)
    assert await ledger.flush(5)
    assert stuck.state == "quarantined" and ledger._mark_failing_since is None


async def test_a_request_that_ends_is_settled_while_a_mark_waits_out_its_back_off(monkeypatch):
    # The mark's wait is its own. Waited out on the writer's timer, which
    # holds every write back until it has run out, it kept a request that
    # ended meanwhile from being settled: for up to half a minute, where a
    # mark kept failing and little else happened.
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 30.0)
    ledger, backend = _ledger()
    backend.fail_quarantine = asyncpg.exceptions.InsufficientPrivilegeError("no UPDATE here")
    stuck = await _refused(ledger, backend, "u-stuck", 0.50)

    async def settled_at_once() -> None:
        await _idle(ledger)
        assert ledger._marking and ledger._retry is None and ledger._mark_retry is not None
        reservation, _ = await ledger.admit(_check(ledger), 0.01)
        reservation.release()
        async with asyncio.timeout(2):
            while reservation.state != "retired":
                await asyncio.sleep(0.005)
        assert stuck.state == "queued"

    # After the mark failed where it was refused...
    await settled_at_once()
    assert backend.quarantine_calls == 1
    assert ledger.stats()["settlement"]["refused_awaiting_mark"] == 1
    # ...and after its timer went off and it failed again, with nothing else to write.
    ledger._mark_retry.cancel()
    ledger._mark_now()
    await settled_at_once()
    assert backend.quarantine_calls == 2

    # Owing a mark, the process is not recorded as having sent everything.
    assert not await ledger.close()
    assert ledger._mark_retry is None and backend.closures == {}


async def test_a_flush_tries_the_marks_at_once_and_then_at_the_shortest_back_off(monkeypatch):
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 30.0)
    ledger, backend = _ledger()
    backend.fail_quarantine = ConnectionRefusedError("the database is away")
    first = await _refused(ledger, backend, "u-first", 0.50)
    await _idle(ledger)
    # Half a minute to wait, and a shutdown that has a second: tried now.
    backend.fail_quarantine = None
    assert await ledger.flush(1)
    assert first.state == "quarantined"

    # One that fails once more during the flush is tried again after a short
    # pause, not when its own back-off (half a minute by now) is over.
    backend.fail_quarantine = ConnectionRefusedError("the database is away")
    second = await _refused(ledger, backend, "u-second", 0.50)
    await _idle(ledger)
    mark, calls = backend.quarantine, 0

    async def fails_once_more(member, instance_id, *, day, note):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionRefusedError("still away")
        return await mark(member, instance_id, day=day, note=note)

    backend.fail_quarantine = None
    monkeypatch.setattr(backend, "quarantine", fails_once_more)
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 0.01)
    assert await ledger.flush(2)
    assert second.state == "quarantined" and calls == 2


async def test_a_mark_that_keeps_failing_starts_the_writer_only_when_it_is_due(monkeypatch):
    # Owed and not yet due is nothing to write. Taken for something to write,
    # it started the writer again the moment it finished, for as long as the
    # mark kept failing.
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 0.05)
    ledger, backend = _ledger()
    runs = 0
    write_batch = ledger._write_batch

    async def counted() -> None:
        nonlocal runs
        runs += 1
        await write_batch()

    monkeypatch.setattr(ledger, "_write_batch", counted)
    backend.fail_quarantine = ConnectionRefusedError("the database is away")
    stuck = await _refused(ledger, backend, "u-stuck", 0.50)
    await asyncio.sleep(0.5)
    # Tried when it was refused, then 0.05, 0.15 and 0.35 s later: the delay doubles.
    assert 2 <= backend.quarantine_calls <= 5
    assert runs <= backend.quarantine_calls + 2
    assert stuck.state == "queued"
    assert not await ledger.close()


async def test_a_pass_over_the_marks_stops_after_a_few_failures(monkeypatch):
    # What a database that refuses every mark costs each time: a few calls,
    # not one for every reservation waiting.
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 30.0)
    ledger, backend = _ledger()
    backend.fail_quarantine = asyncpg.exceptions.InsufficientPrivilegeError("no UPDATE here")
    refused = [await _refused(ledger, backend, f"u-many-{i}", 0.50) for i in range(12)]
    # The first was tried when it was refused; the others found it backing off.
    assert len(ledger._marking) == 12 and backend.quarantine_calls == 1

    ledger._mark_after = 0.0
    await ledger._mark_refused()
    assert backend.quarantine_calls == 1 + shared_ledger._MARK_FAILURES_PER_PASS
    assert len(ledger._marking) == 12 and all(r.state == "queued" for r in refused)

    # And at the first failure that is the database's own: every other mark
    # would wait as long for the same answer, and the writer with them.
    backend.fail_quarantine = TimeoutError("no answer in a minute")
    ledger._mark_after = 0.0
    await ledger._mark_refused()
    assert backend.quarantine_calls == 2 + shared_ledger._MARK_FAILURES_PER_PASS
    assert len(ledger._marking) == 12

    backend.fail_quarantine = None
    assert await ledger.flush(5)
    assert all(r.state == "quarantined" for r in refused) and ledger._mark_retry is None


async def test_a_mark_that_cannot_be_made_does_not_hold_up_the_ones_behind_it(monkeypatch):
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 30.0)
    ledger, backend = _ledger()
    backend.fail_quarantine = asyncpg.exceptions.NumericValueOutOfRangeError("too large")
    first = await _refused(ledger, backend, "u-first", 0.50)
    second = await _refused(ledger, backend, "u-second", 0.50)
    assert [m.reservation_id for m, _, _ in ledger._marking] == [
        first.reservation_id,
        second.reservation_id,
    ]
    # Only the first one's mark fails from here on.
    backend.unmarkable = {first.reservation_id}
    assert not await ledger.flush(0.2)
    assert second.state == "quarantined" and first.state == "queued"
    assert backend.quarantined == [second.reservation_id]
    backend.unmarkable = set()
    backend.fail_quarantine = None
    assert await ledger.flush(5)
    assert first.state == "quarantined"


def test_a_batch_names_each_reservation_once_with_its_own_costs():
    line = CostLine(USER, utc_day(), None, usd(0.1))
    member = Member("sr_1", USER, "settle", costs=(line,))
    assert SettlementBatch.make("i", [member], []).inconsistency() is None
    assert "more than once" in SettlementBatch.make("i", [member, member], []).inconsistency()
    foreign = Member("sr_2", USER, "settle", api_key_id=5, costs=(line,))
    assert "elsewhere" in SettlementBatch.make("i", [foreign], []).inconsistency()
    release = Member("sr_3", USER, "release", costs=(line,))
    assert "carries costs" in SettlementBatch.make("i", [release], []).inconsistency()


def test_the_local_mode_is_the_default_and_keeps_the_per_process_ledger():
    assert spend_mode.current() == "local"
    store = MagicMock()
    assert isinstance(ledger_for(store), SpendLedger)


class _RootStore:
    """A store that keeps the shared accounting, as PostgresOperationalStore does."""

    def __init__(self) -> None:
        self.backend = FakeSharedBackend()

    def shared_spend_root(self):
        return self

    def shared_spend_backend(self):
        return self.backend


def test_the_shared_mode_hands_out_the_shared_ledger(monkeypatch):
    monkeypatch.setattr(spend_mode, "_mode", "shared")
    store = _RootStore()
    assert isinstance(ledger_for(store), SharedSpendLedger)
    check = _check(ledger_for(store))
    with pytest.raises(RuntimeError, match="reserve"):
        check.admit()


def test_the_production_wrapper_reaches_the_same_ledger_as_its_store(monkeypatch):
    from serving.storage.cache import CachedOperationalStore, InMemoryCache

    monkeypatch.setattr(spend_mode, "_mode", "shared")
    inner = _RootStore()
    wrapped = CachedOperationalStore(inner, InMemoryCache())
    assert ledger_for(wrapped) is ledger_for(inner)
    assert ledger_for(wrapped)._backend is inner.backend


def test_a_store_that_cannot_keep_the_shared_accounting_fails_closed(monkeypatch):
    from serving.shared_spend.ledger import SharedSpendUnsupported
    from serving.storage.base import OperationalStore
    from serving.storage.cache import CachedOperationalStore, InMemoryCache

    monkeypatch.setattr(spend_mode, "_mode", "shared")
    plain = MagicMock(spec=OperationalStore)
    for store in (plain, CachedOperationalStore(plain, InMemoryCache())):
        with pytest.raises(SharedSpendUnsupported):
            ledger_for(store)
    # The local mode is unaffected by the protocol.
    monkeypatch.setattr(spend_mode, "_mode", "local")
    assert isinstance(ledger_for(plain), SpendLedger)
