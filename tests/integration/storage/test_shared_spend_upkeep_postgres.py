"""What keeps shared spend accounting from needing attention nobody gives, on real PostgreSQL.

``test_shared_spend_postgres`` covers admission and settlement. This module
covers what is left when a process stops:

- **Reconciliation.** A process that was killed leaves its reservations
  counted. An operator retires them (``python -m serving.shared_spend
  reconcile``): one whose request-log row records a charge is charged that,
  one with none is given back whole, and each gets an audit row. A
  process the registry still hears from is refused, and a process wrongly
  declared gone charges nothing twice when it settles after all.
- **Closure.** A process that sent everything records that it will send
  nothing more.
- **Receipt retention.** A settlement receipt is deleted only when nothing can
  send its batch again: by its own process once the batch was answered, by any
  process a week after its maker closed. Age alone never deletes one.

Request-log rows are written through the real logger and log store, so what
the reconciliation relies on (the row names its reservation, and what was
charged to it) is what production writes.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import asyncpg
import pytest

from serving import spend_ledger
from serving.config.instances import STALE_AFTER_SECONDS
from serving.servers.routers.completions_logging import CompletionsLogger
from serving.shared_spend import reconcile
from serving.shared_spend.__main__ import Refused, run
from serving.shared_spend.ledger import SharedSpendLedger
from serving.shared_spend.store import RECEIPT_RETENTION_SECONDS, OwnerHeardFrom
from serving.storage.postgres_log import PostgresLogStore
from serving.utils import context as req_ctx
from tests.fixtures import postgres
from tests.integration.storage import test_shared_spend_postgres as _base

db = _base.db
check = _base.check
_rows = _base._rows
_members = _base._members
_account = _base._account
_charge = _base._charge

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PREFIX = _base.PREFIX  # "u-ss-": accounts, and their request-log rows, the fixture wipes

#: A week and a day: older than the retention, by the database's clock.
OLD_DAYS = RECEIPT_RETENTION_SECONDS / 86400 + 1


async def _log_request(pool, reservation, user, *, tokens: int = 1000, status: int = 200) -> str:
    """Write the request-log row a request leaves, through the real logger; return its id.

    *tokens* prompt and as many completion tokens at $1 per million each: 1000
    cost $0.002. With none the row has no cost, as a failed request's has.
    """
    store = PostgresLogStore(pool, store_full_prompts=False, fence_secret=postgres.FENCE_SECRET)
    logger = CompletionsLogger(log_store=store)
    request_id = f"req_{uuid.uuid4().hex}"
    usage = (
        {"prompt_tokens": tokens, "completion_tokens": tokens, "total_tokens": 2 * tokens}
        if tokens
        else None
    )
    row = {
        "request_id": request_id,
        "model_id": "m",
        "provider": "p",
        "prompt": [{"role": "user", "content": "hi"}],
        "response": None,
        "usage": usage,
        "latency_ms": 5,
        "status_code": status,
        "metadata": {"user_id": user},
        "pricing": {"prompt": "1", "completion": "1"},
    }
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        logger.schedule_log(request_id, row)
    await logger.stop()
    return request_id


async def _register(pool, instance_id: str, *, lifecycle: str = "running", age: float = 0.0):
    """Register *instance_id* as the registry would know it, last heard from *age* seconds ago."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO config_instances (instance_id, build, capabilities, started_at, "
            "heartbeat_at, lifecycle, report_seq, groups) "
            "VALUES ($1, 'test', '{}'::jsonb, NOW(), NOW() - make_interval(secs => $2), $3, 1, "
            "'{}'::jsonb) ON CONFLICT (instance_id) DO UPDATE SET "
            "heartbeat_at = EXCLUDED.heartbeat_at, lifecycle = EXCLUDED.lifecycle",
            instance_id,
            float(age),
            lifecycle,
        )


async def _reconcile(backend, instance_id: str, **options) -> dict:
    args = SimpleNamespace(
        command="reconcile",
        instance_id=instance_id,
        apply=False,
        operator=None,
        reason=None,
        limit=1000,
    )
    vars(args).update(options)
    return json.loads((await run(args, backend))[0])


async def _audit(pool) -> dict[str, dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM spend_reconciliations")
    return {row["reservation_id"]: dict(row) for row in rows}


async def _closures(pool) -> dict[str, tuple]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT instance_id, closed_by, operator FROM spend_instance_closures"
        )
    return {row["instance_id"]: (row["closed_by"], row["operator"]) for row in rows}


async def _batches(pool) -> dict[str, str]:
    """Every batch receipt kept: batch id -> the instance that made it."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT batch_id, instance_id FROM spend_settlements")
    return {row["batch_id"]: row["instance_id"] for row in rows}


async def _age(pool, table: str, column: str, key: str, values: list[str]) -> None:
    """Make rows of *table* :data:`OLD_DAYS` old."""
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {table} SET {column} = NOW() - make_interval(secs => $2) "
            f"WHERE {key} = ANY($1::text[])",
            values,
            OLD_DAYS * 86400,
        )


async def _age_receipts(pool, batch_ids: list[str]) -> None:
    await _age(pool, "spend_settlements", "settled_at", "batch_id", batch_ids)


async def _age_closures(pool, instance_ids: list[str]) -> None:
    await _age(pool, "spend_instance_closures", "closed_at", "instance_id", instance_ids)


async def _settle_one(db, instance_id: str, user: str) -> str:
    """One request admitted and settled by a process called *instance_id*; its batch id."""
    ledger = SharedSpendLedger(db.a._backend, instance_id=instance_id)
    before = set(await _batches(db.pool))
    reservation, _ = await ledger.admit(check(ledger, user), 0.01)
    reservation.release()
    assert await ledger.flush(5)
    (batch_id,) = set(await _batches(db.pool)) - before
    return batch_id


# -- reconciliation ---------------------------------------------------------------


async def test_what_a_killed_process_left_is_charged_from_the_request_log_or_given_back(db):
    user = PREFIX + "killed"
    crashed = SharedSpendLedger(db.a._backend, instance_id="inst-crashed")
    logged, _ = await crashed.admit(check(crashed, user), 0.30)
    failed, _ = await crashed.admit(check(crashed, user), 0.30)
    cut_off, _ = await crashed.admit(check(crashed, user), 0.30)
    # One request ended, was charged by its billing point and logged. One
    # failed upstream after it had used tokens: its row prices them, and its
    # billing point charged nothing. One was still running.
    _charge(crashed, logged, 0.002)
    request_id = await _log_request(db.pool, logged, user)
    await _log_request(db.pool, failed, user, status=502)
    del crashed  # killed: nothing settles these, nothing expires them

    held, _ = await db.b.admit(check(db.b, user), 1.0)
    assert held.amount == Decimal("0.100000")  # $0.90 of the $1.00 is still held
    held.release()
    assert await db.b.flush(5)

    backend = db.b._backend
    plan = await _reconcile(backend, "inst-crashed")
    assert plan["owner"] == "not registered" and plan["reconcilable"] and not plan["applied"]
    assert (plan["reservations"], plan["to_charge"], plan["to_release"]) == (3, 1, 2)
    assert plan["held_amount"] == "0.900000" and plan["charge_amount"] == "0.002000"
    by_id = {entry["reservation_id"]: entry for entry in plan["plan"]}
    assert by_id[logged.reservation_id]["would"] == "charge"
    assert by_id[logged.reservation_id]["request_log"][0]["request_id"] == request_id
    assert by_id[logged.reservation_id]["request_log"][0]["charged_amount"] == "0.002000"
    # The failed request's row carries a price; what was charged is what counts.
    assert by_id[failed.reservation_id]["would"] == "release"
    failed_row = by_id[failed.reservation_id]["request_log"][0]
    assert (failed_row["status_code"], failed_row["charged_amount"]) == (502, "0.000000")
    assert Decimal(failed_row["cost_amount"]) == Decimal("0.002")
    assert by_id[cut_off.reservation_id]["request_log"] == []
    assert user not in json.dumps(plan, default=str)  # no account is named
    assert len(await _rows(db.pool, user)) == 3  # showing the plan changes nothing
    assert await _audit(db.pool) == {}

    with pytest.raises(Refused, match="--operator"):
        await _reconcile(backend, "inst-crashed", apply=True)
    with pytest.raises(Refused, match="--reason"):
        await _reconcile(backend, "inst-crashed", apply=True, operator="alice", reason="  ")

    done = await _reconcile(
        backend,
        "inst-crashed",
        apply=True,
        operator="alice",
        reason="host rebooted at 03:10; the process is gone",
    )
    assert (done["charged"], done["released"]) == (1, 2)
    assert done["charge_amount"] == "0.002000" and done["released_amount"] == "0.600000"
    assert done["not_retired"] == {} and done["closed_now"] is True
    assert (done["active_left"], done["quarantined_left"]) == (0, 0)

    # Retired, the one charge on the account's day, each retirement recorded.
    assert await _rows(db.pool, user) == []
    assert await _account(db.pool, user) == Decimal("0.002")
    members = await _members(db.pool)
    assert members[logged.reservation_id] == "settled"
    assert members[failed.reservation_id] == members[cut_off.reservation_id] == "released"

    audit = await _audit(db.pool)
    assert set(audit) == {logged.reservation_id, failed.reservation_id, cut_off.reservation_id}
    row = audit[logged.reservation_id]
    assert (row["outcome"], row["cost_amount"], row["amount"]) == (
        "settled",
        Decimal("0.002"),
        Decimal("0.3"),
    )
    assert (row["instance_id"], row["operator"], row["owner_state"]) == (
        "inst-crashed",
        "alice",
        "not registered",
    )
    assert "rebooted" in row["reason"] and row["batch_id"] in done["batches"]
    evidence = json.loads(row["evidence"])
    assert [e["request_id"] for e in evidence] == [request_id]
    assert evidence[0]["charged_amount"] == "0.002000"
    assert audit[failed.reservation_id]["outcome"] == "released"
    assert json.loads(audit[failed.reservation_id]["evidence"])[0]["status_code"] == 502
    assert json.loads(audit[cut_off.reservation_id]["evidence"]) == []
    assert "user_id" not in row and user not in json.dumps(audit, default=str)

    assert (await _closures(db.pool))["inst-crashed"] == ("operator", "alice")
    # The account has its budget back, less the one real charge.
    after, _ = await db.b.admit(check(db.b, user), 1.0)
    assert after.amount == Decimal("0.998000")
    status = json.loads((await run(SimpleNamespace(command="status"), backend))[0])
    assert status["reconciled"]["reservations"] == 3
    assert Decimal(str(status["reconciled"]["cost_amount"])) == Decimal("0.002")
    assert status["closures"] == {"operator": 1}
    assert "inst-crashed" not in status["stranded"]["instances"]


async def test_a_cost_recorded_before_midnight_is_restored_to_the_day_it_was_recorded_on(
    db, monkeypatch
):
    """A cost recorded on one UTC day, its request logged on the next, its process then killed.

    The process's own settlement counts a cost on the day its billing point
    recorded it. The reconciliation counted it on the day the request's log
    row was written: the same total, taken from the next day's budget -- and
    from the next month's, on a month's last day. The row now says on which
    days the charge was recorded, and the reconciliation restores it there.
    """
    user = PREFIX + "midnight"
    today = spend_ledger.utc_day()
    yesterday = spend_ledger.utc_day(datetime.now(UTC) - timedelta(days=1))

    def charge_yesterday(ledger, reservation, cost: float) -> None:
        with monkeypatch.context() as at:
            at.setattr(spend_ledger, "utc_day", lambda now=None: yesterday)
            _charge(ledger, reservation, cost)

    # What a process does itself with such a request: charged yesterday,
    # logged and settled today.
    own, _ = await db.a.admit(check(db.a, user), 0.30)
    charge_yesterday(db.a, own, 0.20)
    await _log_request(db.pool, own, user)
    own.release()
    assert await db.a.flush(5)
    assert await _account(db.pool, user, yesterday) == Decimal("0.2")
    assert await _account(db.pool, user, today) == 0

    # The same request left by a process that was killed before settling it,
    # and one billed on both sides of midnight.
    crashed = SharedSpendLedger(db.a._backend, instance_id="inst-crashed")
    left, _ = await crashed.admit(check(crashed, user), 0.30)
    charge_yesterday(crashed, left, 0.20)
    await _log_request(db.pool, left, user)
    both, _ = await crashed.admit(check(crashed, user), 0.30)
    charge_yesterday(crashed, both, 0.10)
    _charge(crashed, both, 0.05)
    await _log_request(db.pool, both, user)
    del crashed

    backend = db.b._backend
    plan = await _reconcile(backend, "inst-crashed")
    by_id = {entry["reservation_id"]: entry for entry in plan["plan"]}
    assert by_id[left.reservation_id]["request_log"][0]["day"] == today  # the row is today's
    assert by_id[left.reservation_id]["charge_days"] == {yesterday: "0.200000"}
    assert by_id[both.reservation_id]["charge_days"] == {
        yesterday: "0.100000",
        today: "0.050000",
    }

    done = await _reconcile(
        backend, "inst-crashed", apply=True, operator="alice", reason="killed at midnight"
    )
    assert (done["charged"], done["charge_amount"]) == (2, "0.350000")
    # Where the process's own settlement would have put them.
    assert await _account(db.pool, user, yesterday) == Decimal("0.5")
    assert await _account(db.pool, user, today) == Decimal("0.05")
    audit = await _audit(db.pool)
    evidence = json.loads(audit[left.reservation_id]["evidence"])
    assert evidence[0]["charged_days"] == {yesterday: "0.200000"}
    assert audit[both.reservation_id]["cost_amount"] == Decimal("0.15")


async def test_a_process_the_registry_hears_from_is_refused(db):
    user = PREFIX + "alive"
    await _register(db.pool, "inst-a")
    running, _ = await db.a.admit(check(db.a, user), 0.30)
    backend = db.b._backend

    plan = await _reconcile(backend, "inst-a")
    assert plan["owner"] == "running" and plan["reconcilable"] is False
    with pytest.raises(Refused, match="retires its own"):
        await _reconcile(backend, "inst-a", apply=True, operator="alice", reason="looked dead")
    # The statement refuses too, whatever the caller checked first.
    planned = await reconcile.plan(backend, "inst-a")
    with pytest.raises(OwnerHeardFrom):
        await backend.reconcile("inst-a", planned, operator="alice", reason="looked dead")
    assert len(await _rows(db.pool, user)) == 1
    assert await _audit(db.pool) == {} and await _closures(db.pool) == {}

    # Not heard from for longer than a heartbeat may be late: then it may be.
    await _register(db.pool, "inst-a", age=STALE_AFTER_SECONDS + 30)
    plan = await _reconcile(backend, "inst-a")
    assert plan["owner"] == "stale" and plan["reconcilable"] is True
    # And one that reported itself stopped.
    await _register(db.pool, "inst-a", lifecycle="stopped")
    assert (await _reconcile(backend, "inst-a"))["owner"] == "stopped"

    running.release()
    assert await db.a.flush(5)


async def test_an_instance_nothing_names_is_refused_and_leaves_no_closure(db):
    backend = db.b._backend
    plan = await _reconcile(backend, "inst-typo")
    assert plan["known"] is False and plan["reconcilable"] is False
    with pytest.raises(Refused, match="nothing names instance inst-typo"):
        await _reconcile(backend, "inst-typo", apply=True, operator="alice", reason="a typo")
    assert await _closures(db.pool) == {}
    # One that only left receipts behind is known, and may be closed.
    await _settle_one(db, "inst-left-receipts", PREFIX + "known")
    done = await _reconcile(
        backend, "inst-left-receipts", apply=True, operator="alice", reason="host is gone"
    )
    assert done["closed_now"] is True and (done["charged"], done["released"]) == (0, 0)


async def test_a_reservation_with_several_log_rows_is_charged_its_latest_total_once(db):
    user = PREFIX + "rows"
    gone = SharedSpendLedger(db.a._backend, instance_id="inst-rows")
    reservation, _ = await gone.admit(check(gone, user), 0.30)
    # Two billed calls under one reservation, each logged after its charge:
    # the rows record $0.002, then the total, $0.005.
    _charge(gone, reservation, 0.002)
    await _log_request(db.pool, reservation, user)
    _charge(gone, reservation, 0.003)
    latest = await _log_request(db.pool, reservation, user)
    del gone

    plan = await _reconcile(db.b._backend, "inst-rows")
    (entry,) = plan["plan"]
    assert [row["charged_amount"] for row in entry["request_log"]] == ["0.002000", "0.005000"]
    assert entry["charge_amount"] == "0.005000"
    done = await _reconcile(
        db.b._backend, "inst-rows", apply=True, operator="alice", reason="process gone"
    )
    assert (done["charged"], done["charge_amount"]) == (1, "0.005000")
    assert await _account(db.pool, user) == Decimal("0.005")  # the total, once
    (audit,) = (await _audit(db.pool)).values()
    assert [e["request_id"] for e in json.loads(audit["evidence"])][-1] == latest


async def test_a_process_wrongly_declared_gone_charges_nothing_twice_when_it_settles(db, caplog):
    user = PREFIX + "wrong"
    reservation, _ = await db.a.admit(check(db.a, user), 0.30)
    _charge(db.a, reservation, 0.002)  # its billing point...
    await _log_request(db.pool, reservation, user)  # ...and its request-log row, recording it
    # An operator, wrongly: these test ledgers never register, so nothing stops them.
    done = await _reconcile(
        db.b._backend, "inst-a", apply=True, operator="bob", reason="mistaken for a dead one"
    )
    assert done["charged"] == 1
    assert await _account(db.pool, user) == Decimal("0.002")

    # The process is alive after all, and settles the same request.
    reservation.release()
    assert await db.a.flush(5)
    assert await _account(db.pool, user) == Decimal("0.002")  # once
    assert reservation.state == "retired"
    assert db.a.holdings()["unaccounted"] == 0 and db.a.fully_settled()
    # What the process had recorded was not added, and it says so: the one
    # place that tells how much went uncharged if the two ever differ.
    assert db.a.stats()["settlement"]["members_duplicate"] == 1
    (dropped,) = [
        r for r in caplog.records if getattr(r, "event", None) == "spend_settlement_duplicate"
    ]
    assert dropped.reservations == [reservation.reservation_id]
    assert dropped.amount == "0.002000"


class RefusesChargeOf:
    """The real backend, whose counters refuse the charge of one reservation (as an overflow does)."""

    def __init__(self, backend, reservation_id: str) -> None:
        self._backend = backend
        self._refused = reservation_id

    def __getattr__(self, name):
        return getattr(self._backend, name)

    async def reconcile(self, instance_id, plan, **kwargs):
        if any(e.reservation_id == self._refused and e.member.costs for e in plan):
            raise asyncpg.exceptions.NumericValueOutOfRangeError("numeric field overflow")
        return await self._backend.reconcile(instance_id, plan, **kwargs)


async def test_a_charge_the_counters_refuse_does_not_keep_the_rest_from_being_retired(db):
    user = PREFIX + "refused"
    gone = SharedSpendLedger(db.a._backend, instance_id="inst-refused")
    fine, _ = await gone.admit(check(gone, user), 0.20)
    bad, _ = await gone.admit(check(gone, user), 0.20)
    cut_off, _ = await gone.admit(check(gone, user), 0.20)
    for reservation in (fine, bad):
        _charge(gone, reservation, 0.002)
        await _log_request(db.pool, reservation, user)
    del gone

    backend = RefusesChargeOf(db.b._backend, bad.reservation_id)
    done = await _reconcile(
        backend, "inst-refused", apply=True, operator="alice", reason="process gone"
    )
    # The other two are retired, one charged and one given back; the refused
    # one is left as it was and named, and the instance is not called closed.
    assert (done["charged"], done["released"]) == (1, 1)
    assert done["not_retired"] == {"refused": [bad.reservation_id]}
    assert "refused the charge of 1" in done["stopped"] and "closed_now" not in done
    assert (done["active_left"], done["quarantined_left"]) == (1, 0)
    (left,) = await _rows(db.pool, user)
    assert (left["reservation_id"], left["state"]) == (bad.reservation_id, "active")
    assert set(await _audit(db.pool)) == {fine.reservation_id, cut_off.reservation_id}
    assert await _account(db.pool, user) == Decimal("0.002")
    assert await _closures(db.pool) == {}
    # Once the counters take it, a second run finishes the job.
    done = await _reconcile(
        db.b._backend, "inst-refused", apply=True, operator="alice", reason="process gone"
    )
    assert (done["charged"], done["closed_now"]) == (1, True)
    assert await _account(db.pool, user) == Decimal("0.004")


async def test_the_command_refuses_a_fence_secret_the_database_was_not_pinned_to(db):
    from serving.shared_spend.__main__ import require_pinned_secret
    from serving.storage.erasure_fence import validate_or_init_fingerprint

    async with db.pool.acquire() as conn:
        # As a gateway's start does: the test database's one secret.
        await validate_or_init_fingerprint(conn, secret=postgres.FENCE_SECRET)
    await require_pinned_secret(db.pool, postgres.FENCE_SECRET)
    with pytest.raises(Refused, match="not the one this database's fence was made with"):
        await require_pinned_secret(db.pool, "another-secret-from-some-shell")


async def test_a_quarantined_reservation_is_left_to_its_own_resolution(db):
    user = PREFIX + "quarantined"
    gone = SharedSpendLedger(db.a._backend, instance_id="inst-gone")
    refused, _ = await gone.admit(check(gone, user), 0.30)
    await _base.end_quarantined(gone, refused, 0.50)
    stranded, _ = await gone.admit(check(gone, user), 0.20)
    del gone

    done = await _reconcile(
        db.b._backend, "inst-gone", apply=True, operator="alice", reason="process gone"
    )
    assert (done["charged"], done["released"]) == (0, 1)
    assert (done["active_left"], done["quarantined_left"]) == (0, 1)
    (left,) = await _rows(db.pool, user)
    assert (left["reservation_id"], left["state"]) == (refused.reservation_id, "quarantined")
    assert set(await _audit(db.pool)) == {stranded.reservation_id}


# -- closure ----------------------------------------------------------------------


async def test_a_clean_shutdown_records_the_closure_and_a_later_reconciliation_keeps_it(db):
    user = PREFIX + "shutdown"
    reservation, _ = await db.a.admit(check(db.a, user), 0.30)
    reservation.release()
    assert await db.a.flush(5)
    assert await db.a.close() is True
    assert await _closures(db.pool) == {"inst-a": ("shutdown", None)}
    assert (await db.b._backend.owner_states())["inst-a"] == "closed"

    done = await _reconcile(
        db.b._backend, "inst-a", apply=True, operator="alice", reason="tidying up"
    )
    assert done["owner"] == "closed" and done["closed_now"] is False
    assert (done["charged"], done["released"]) == (0, 0)
    assert await _closures(db.pool) == {"inst-a": ("shutdown", None)}  # its own word stands


async def test_an_operators_closure_names_who_and_why(db):
    backend = db.a._backend
    with pytest.raises(asyncpg.CheckViolationError):
        await backend.close_instance("inst-x", closed_by="operator")
    assert await backend.close_instance(
        "inst-x", closed_by="operator", operator="alice", reason="host decommissioned"
    )
    assert not await backend.close_instance("inst-x", closed_by="shutdown")  # the first stands
    assert await _closures(db.pool) == {"inst-x": ("operator", "alice")}


# -- receipt retention --------------------------------------------------------------


async def test_a_process_deletes_its_own_old_receipts_except_those_it_may_send_again(db):
    user = PREFIX + "own"
    mine = [await _settle_one(db, "inst-mine", user) for _ in range(3)]
    theirs = await _settle_one(db, "inst-theirs", user)
    old_answered, old_unanswered, young = mine
    await _age_receipts(db.pool, [old_answered, old_unanswered, theirs])
    backend = db.a._backend

    assert await backend.purge_receipts("inst-mine", [old_unanswered]) == (1, 1)
    # Its unanswered batch and its young one stay; so does another process's,
    # however old: that process has not closed, and may send it again.
    assert set(await _batches(db.pool)) == {old_unanswered, young, theirs}
    assert len(await _members(db.pool)) == 3
    assert await backend.purge_receipts("inst-mine", [old_unanswered]) == (0, 0)
    # Once answered it goes too.
    assert await backend.purge_receipts("inst-mine", []) == (1, 1)
    assert set(await _batches(db.pool)) == {young, theirs}


async def test_the_receipts_of_a_closed_process_are_deleted_a_week_after_it_closed(db):
    user = PREFIX + "closed"
    backend = db.a._backend
    batches = {
        name: await _settle_one(db, name, user)
        for name in ("inst-closed", "inst-recent", "inst-open", "inst-back")
    }
    await _age_receipts(db.pool, list(batches.values()))
    # Closed a week ago; closed yesterday; never closed; and one an operator
    # closed a week ago that the registry hears from all the same.
    await backend.close_instance("inst-closed", closed_by="shutdown")
    await backend.close_instance("inst-recent", closed_by="shutdown")
    await backend.close_instance(
        "inst-back", closed_by="operator", operator="bob", reason="mistaken for a dead one"
    )
    await _age_closures(db.pool, ["inst-closed", "inst-back"])
    await _register(db.pool, "inst-back")

    assert await backend.purge_receipts("inst-someone-else", []) == (1, 1)
    assert set((await _batches(db.pool)).values()) == {"inst-recent", "inst-open", "inst-back"}

    # The one that came back is not heard from any more: then its closure counts.
    await _register(db.pool, "inst-back", age=STALE_AFTER_SECONDS + 30)
    assert await backend.purge_receipts("inst-someone-else", []) == (1, 1)
    assert set((await _batches(db.pool)).values()) == {"inst-recent", "inst-open"}
    # A purge deletes at most what it is asked to at once.
    await _age_closures(db.pool, ["inst-recent"])
    assert await backend.purge_receipts("inst-someone-else", [], limit=0) == (0, 0)
    assert await backend.purge_receipts("inst-someone-else", [], limit=5) == (1, 1)


async def test_a_closure_goes_once_its_process_left_nothing_behind(db):
    user = PREFIX + "closures"
    backend = db.a._backend
    await _settle_one(db, "inst-receipts", user)  # still has a receipt
    holder = SharedSpendLedger(backend, instance_id="inst-held")
    await holder.admit(check(holder, user), 0.01)  # still holds a reservation
    for name in ("inst-empty", "inst-receipts", "inst-held", "inst-new"):
        await backend.close_instance(name, closed_by="shutdown")
    await backend.close_instance("inst-op", closed_by="operator", operator="al", reason="gone")
    await _age_closures(db.pool, ["inst-empty", "inst-receipts", "inst-held", "inst-op"])

    assert await backend.purge_closures() == 1
    # Kept: receipts left, a reservation left, an operator's (the audit), a recent one.
    assert set(await _closures(db.pool)) == {"inst-receipts", "inst-held", "inst-op", "inst-new"}


class AnswerLost:
    """The real backend, whose first settlement commits and then loses its answer."""

    def __init__(self, backend) -> None:
        self._backend = backend
        self.lost = False

    def __getattr__(self, name):
        return getattr(self._backend, name)

    async def settle(self, batch, **kwargs):
        result = await self._backend.settle(batch, **kwargs)
        if not self.lost:
            self.lost = True
            raise ConnectionResetError("the answer was lost")
        return result


async def test_a_batch_not_answered_keeps_its_receipt_through_the_upkeep_and_is_counted_once(db):
    user = PREFIX + "replay"
    ledger = db.a
    real, ledger._backend = ledger._backend, AnswerLost(ledger._backend)
    try:
        reservation, _ = await ledger.admit(check(ledger, user), 0.30)
        _charge(ledger, reservation, 0.20)
        reservation.release()

        async def waiting_to_send_again() -> bool:
            return ledger._retry is not None and ledger._writer is None

        await _base._until(waiting_to_send_again)
        ledger._retry.cancel()  # the test sends it again itself, below
        ledger._retry = None
        assert await _account(db.pool, user) == Decimal("0.2")  # it did commit
        (batch_id,) = [batch.batch_id for batch in ledger._unsettled_batches()]
        await _age_receipts(db.pool, [batch_id])  # ...more than a week ago, say

        await ledger._keep_up()  # the hourly upkeep
        assert batch_id in await _batches(db.pool)  # kept: it may be sent again
        assert ledger.stats()["receipts_purged"] == 0

        assert await ledger.flush(5)  # sent again
        assert await _account(db.pool, user) == Decimal("0.2")  # recognised: counted once
        assert reservation.state == "retired" and ledger.fully_settled()

        await ledger._keep_up()  # answered now, and old
        assert batch_id not in await _batches(db.pool)
        assert await _members(db.pool) == {}
        stats = ledger.stats()
        assert stats["receipts_purged"] == 1
        assert stats["database"]["stranded"]["reservations"] == 0
        # The survey's receipt counts are the planner's estimates (none at all
        # on a table never analyzed); the status command counts.
        assert stats["database"]["receipts"]["estimated"] is True
        assert (await real.summary())["receipts"]["batches"] == 0
    finally:
        ledger._backend = real


async def test_pre_native_usd_log_reconciles_its_legacy_charge_and_day(db):
    user = PREFIX + "legacy-usd"
    reservation, _ = await db.a.admit(check(db.a, user), 0.3)
    _charge(db.a, reservation, 0.2)
    request_id = await _log_request(db.pool, reservation, user)
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE api_logs SET metadata = (metadata - 'spend_charged_amount' - 'spend_currency') "
            "|| jsonb_build_object('spend_charged_days', "
            "(SELECT jsonb_agg(day - 'amount') FROM jsonb_array_elements(metadata->'spend_charged_days') day)) "
            "WHERE request_id=$1",
            request_id,
        )
    done = await _reconcile(
        db.b._backend,
        "inst-a",
        apply=True,
        operator="synthetic",
        reason="legacy USD writer is gone",
    )
    assert done["charged"] == 1 and done["charge_amount"] == "0.200000"
    assert await _account(db.pool, user) == Decimal("0.2")
