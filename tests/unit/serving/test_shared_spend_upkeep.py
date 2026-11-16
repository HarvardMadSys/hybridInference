"""What the shared ledger reports, closes and cleans up, on an in-memory backend.

The numbers it keeps (``SharedSpendLedger.stats``), the alerts it asks for when
the database stops answering or reservations are stranded, the closure a clean
shutdown records, and the hourly upkeep. What the statements behind them do is
tested against PostgreSQL in
``tests/integration/storage/test_shared_spend_upkeep_postgres.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from serving import spend_ledger
from serving.shared_spend import ledger as shared_ledger
from serving.shared_spend.ledger import SharedSpendLedger, SpendAdmissionUnavailable
from serving.shared_spend.stats import LATENCY_BUCKETS, Latency, stranded, survey
from serving.shared_spend.store import PURGE_BATCHES, owner_state
from serving.spend_ledger import SpendCheck
from serving.utils import context as req_ctx
from tests.fixtures.shared_spend import FakeSharedBackend

USER = "u-upkeep"


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


async def _until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


@pytest.fixture
def alerts(monkeypatch) -> list[tuple[str, dict[str, Any], str]]:
    """The alerts the ledger asks for, as (title, context, dedupe key)."""
    sent: list[tuple[str, dict[str, Any], str]] = []

    def alert_slack(severity, title, context, *, dedupe_key, cooldown_sec):
        sent.append((title, context, dedupe_key))

    module = types.SimpleNamespace(
        AlertSeverity=types.SimpleNamespace(ERROR="error"), alert_slack=alert_slack
    )
    monkeypatch.setattr(spend_ledger, "_send_alert", lambda send: send(module))
    return sent


def _keys(alerts) -> list[str]:
    return [key for _, _, key in alerts]


async def _run_upkeep(ledger: SharedSpendLedger) -> None:
    ledger._upkeep_at = 0.0  # due now
    ledger._start_upkeep()
    task = ledger._upkeep
    assert task is not None
    await task


# -- the numbers --------------------------------------------------------------


def test_a_latency_is_counted_in_its_bucket_and_read_as_an_upper_bound():
    latency = Latency()
    assert latency.at_most(0.5) is None and latency.snapshot()["mean_seconds"] is None
    for seconds in (0.0004, 0.004, 0.004, 0.004, 0.004, 0.004, 0.004, 0.004, 0.03, 4.0):
        latency.observe(seconds)
    snapshot = latency.snapshot()
    assert snapshot["count"] == 10
    assert snapshot["buckets_seconds"]["0.001"] == 1
    assert snapshot["buckets_seconds"]["0.005"] == 7
    assert snapshot["buckets_seconds"]["0.05"] == 1
    assert snapshot["buckets_seconds"]["more"] == 1  # slower than every bound
    assert sum(snapshot["buckets_seconds"].values()) == 10
    assert snapshot["p50_at_most_seconds"] == 0.005
    assert snapshot["p95_at_most_seconds"] == 4.0  # in the open bucket: the longest seen
    assert snapshot["longest_seconds"] == 4.0
    assert snapshot["mean_seconds"] == pytest.approx(0.40584)
    # An observation exactly on a bound belongs to that bound's bucket.
    exact = Latency()
    exact.observe(LATENCY_BUCKETS[0])
    assert exact.counts[0] == 1


async def test_admissions_are_counted_by_how_they_ended_and_timed():
    ledger, backend = _ledger()
    first, _ = await ledger.admit(_check(ledger), 0.30)
    assert first is not None
    nothing, refusal = await ledger.admit(_check(ledger, quota=0.30), 0.30)
    assert nothing is None and refusal is not None
    backend.fail_admit = TimeoutError()
    with pytest.raises(SpendAdmissionUnavailable):
        await ledger.admit(_check(ledger), 0.30)

    stats = ledger.stats()
    assert stats["admissions"] == {"admitted": 1, "refused": 1, "unavailable": 1}
    assert stats["admission_latency"]["count"] == 3
    assert stats["admission_latency"]["p99_at_most_seconds"] is not None
    assert stats["instance_id"] == "inst-unit" and stats["uptime_seconds"] >= 0
    assert stats["database"] is None  # no survey yet


async def test_what_is_held_is_reported_with_its_amount_and_how_long_it_has_been():
    ledger, backend = _ledger()
    running, _ = await ledger.admit(_check(ledger), 0.30)
    ended, _ = await ledger.admit(_check(ledger), 0.20)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: ended}):
        ledger.record(USER, 0.45)  # more than it reserved: that is what it holds
    backend.settle_gate = asyncio.Event()  # its settlement gets no answer yet
    ended.release()
    await asyncio.wait_for(backend.settling.wait(), 5)

    held = ledger.stats()["held"]
    assert held["active"]["reservations"] == 1
    assert held["active"]["amount_usd"] == pytest.approx(0.30)
    assert held["active"]["oldest_seconds"] >= 0
    assert held["awaiting_settlement"]["reservations"] == 1
    assert held["awaiting_settlement"]["amount_usd"] == pytest.approx(0.45)
    assert held["awaiting_settlement"]["oldest_seconds"] >= 0
    assert held["quarantined"] == {"reservations": 0, "amount_usd": 0.0, "oldest_seconds": None}
    assert ledger.stats()["settlement"]["unwritten_usd"] == pytest.approx(0.45)

    backend.settle_gate.set()
    assert await ledger.flush(5)
    running.release()
    assert await ledger.flush(5)
    held = ledger.stats()["held"]
    assert held["active"]["reservations"] == held["awaiting_settlement"]["reservations"] == 0
    assert ledger.stats()["settlement"]["batches_answered"] == 2


# -- the alerts ----------------------------------------------------------------


async def test_an_admission_the_database_did_not_answer_is_alerted_at_most_once_a_minute(alerts):
    ledger, backend = _ledger()
    backend.fail_admit = TimeoutError()
    for _ in range(3):
        with pytest.raises(SpendAdmissionUnavailable):
            await ledger.admit(_check(ledger), 0.30)
    assert _keys(alerts) == ["spend_admission_unavailable"]
    assert alerts[0][1]["refused_since_start"] == 1

    ledger._unavailable_alert_at -= shared_ledger._UNAVAILABLE_ALERT_SECONDS
    with pytest.raises(SpendAdmissionUnavailable):
        await ledger.admit(_check(ledger), 0.30)
    assert _keys(alerts) == ["spend_admission_unavailable"] * 2
    assert alerts[1][1]["refused_since_start"] == 4
    assert ledger.stats()["admissions"] == {"unavailable": 4}


async def test_a_settlement_failing_for_a_minute_is_reported_once_until_one_is_answered(
    monkeypatch, alerts, caplog
):
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(shared_ledger, "_RETRY_MAX_SECONDS", 0.02)
    ledger, backend = _ledger()
    backend.fail_settle = ConnectionRefusedError("the database is restarting")
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    reservation.release()

    await _until(lambda: ledger._stats.write_failures >= 2)
    assert _keys(alerts) == []  # failing, but not for long yet
    assert ledger.stats()["settlement"]["failing_for_seconds"] is not None

    ledger._failing_since -= shared_ledger.SETTLEMENT_STALLED_SECONDS  # as if a minute ago
    await _until(lambda: bool(alerts))
    failures = ledger._stats.write_failures
    await _until(lambda: ledger._stats.write_failures >= failures + 3)
    assert _keys(alerts) == ["spend_settlement_stalled"]  # once for the stall
    title, context, _ = alerts[0]
    assert title == "Spend settlement is failing"
    assert context["reservations_waiting"] == 1 and context["amount_held_usd"] == "0.3"
    assert context["error"] == "ConnectionRefusedError"
    stalled = [r for r in caplog.records if getattr(r, "event", None) == "spend_settlement_stalled"]
    assert len(stalled) == 1 and stalled[0].reservations == 1
    settlement = ledger.stats()["settlement"]
    assert settlement["failing_for_seconds"] >= shared_ledger.SETTLEMENT_STALLED_SECONDS
    assert settlement["last_error"] == "ConnectionRefusedError"
    assert ledger.stats()["held"]["awaiting_settlement"]["reservations"] == 1

    backend.fail_settle = None
    assert await ledger.flush(5)
    settlement = ledger.stats()["settlement"]
    assert settlement["failing_for_seconds"] is None and settlement["last_error"] is None
    # A later stall is a new one, reported again.
    backend.fail_settle = ConnectionRefusedError("again")
    ledger.record(USER, 0.10)
    await _until(lambda: ledger._failing_since is not None)
    ledger._failing_since -= shared_ledger.SETTLEMENT_STALLED_SECONDS
    await _until(lambda: len(alerts) == 2)
    assert _keys(alerts) == ["spend_settlement_stalled"] * 2
    backend.fail_settle = None
    assert await ledger.flush(5)


async def test_an_entry_the_counters_refuse_is_an_answer_not_a_stall(alerts):
    ledger, backend = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(USER, 0.50)
    backend.fail_settle = asyncpg.exceptions.NumericValueOutOfRangeError("numeric field overflow")
    reservation.release()
    assert await ledger.flush(5)
    settlement = ledger.stats()["settlement"]
    assert settlement["entries_refused"] == 1 and settlement["write_failures"] == 0
    assert settlement["failing_for_seconds"] is None
    assert _keys(alerts) == ["spend_settlement_refused"]
    assert ledger.stats()["held"]["quarantined"]["amount_usd"] == pytest.approx(0.50)


# -- closure --------------------------------------------------------------------


async def test_a_process_that_sent_everything_records_its_closure_and_sends_nothing_after():
    ledger, backend = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    reservation.release()
    assert await ledger.flush(5)
    assert await ledger.close() is True
    assert backend.closures == {"inst-unit": "shutdown"}

    ledger.record(USER, 0.10)  # a cost recorded too late to be sent
    await asyncio.sleep(0.02)
    assert ledger._writer is None and len(backend.batches) == 1
    assert ledger.unwritten() == (pytest.approx(0.10), 1)  # reported, not lost from view
    assert await ledger.close() is False  # and no longer "everything sent"


async def test_a_process_with_something_unsent_is_not_recorded_as_closed(monkeypatch):
    monkeypatch.setattr(shared_ledger, "_RETRY_FIRST_SECONDS", 0.01)
    ledger, backend = _ledger()
    backend.fail_settle = ConnectionRefusedError("the database is down")
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    reservation.release()
    assert not await ledger.flush(0.05)
    assert await ledger.close() is False
    assert backend.closures == {}
    # It stops trying: the batch it could not send stays for an operator.
    await _until(lambda: ledger._writer is None)
    assert ledger._retry is None
    assert ledger.holdings()["queued"] == 1


async def test_a_process_with_a_request_still_running_is_not_recorded_as_closed():
    # Closed, it sends nothing more: the running request's settlement would
    # never be written, under an instance that says it left nothing behind.
    ledger, backend = _ledger()
    running, _ = await ledger.admit(_check(ledger), 0.30)
    assert await ledger.flush(1)  # nothing is waiting to be sent
    assert await ledger.close() is False
    assert backend.closures == {}
    assert ledger.holdings()["active"] == 1 and running.state == "active"


async def test_a_closure_the_database_cannot_record_is_reported_as_not_closed():
    ledger, backend = _ledger()
    backend.fail_close = ConnectionRefusedError("the database is down")
    assert await ledger.close() is False
    assert backend.closures == {}


async def test_shutdown_closes_a_shared_ledger_and_says_so_when_it_could_not(monkeypatch, caplog):
    monkeypatch.setattr(shared_ledger, "_LEDGERS", {})
    clean, clean_backend = _ledger()
    stuck = SharedSpendLedger(FakeSharedBackend(), instance_id="inst-stuck")
    stuck._backend.fail_close = ConnectionRefusedError("the database is down")
    shared_ledger._LEDGERS.update({"clean": clean, "stuck": stuck})
    monkeypatch.setattr(spend_ledger, "_LEDGERS", {})
    monkeypatch.setattr(spend_ledger, "_PINNED", {})

    await spend_ledger.flush_all(timeout=1)

    assert clean_backend.closures == {"inst-unit": "shutdown"}
    not_closed = [
        r for r in caplog.records if getattr(r, "event", None) == "spend_instance_not_closed"
    ]
    assert len(not_closed) == 1


# -- upkeep ----------------------------------------------------------------------


async def test_the_upkeep_keeps_the_receipts_of_batches_not_answered_yet_and_runs_hourly():
    ledger, backend = _ledger()
    backend.settle_gate = asyncio.Event()  # a batch that gets no answer yet
    ledger.record(USER, 0.25)
    await asyncio.wait_for(backend.settling.wait(), 5)
    unanswered = ledger._writing.batch_id
    backend.purge_answers = [(PURGE_BATCHES, 900), (3, 4)]  # a full chunk, then the rest

    await _run_upkeep(ledger)

    assert backend.purges == [("inst-unit", [unanswered])] * 2
    assert ledger.stats()["receipts_purged"] == PURGE_BATCHES + 3
    assert ledger._upkeep is None
    assert ledger._upkeep_at >= time.monotonic() + shared_ledger.UPKEEP_SECONDS - 5
    ledger._start_upkeep()  # not due again for an hour
    assert ledger._upkeep is None and len(backend.purges) == 2

    backend.settle_gate.set()
    assert await ledger.flush(5)


async def test_an_upkeep_that_could_not_delete_everything_comes_again_within_a_minute():
    # A busy process makes a receipt for nearly every request: one run deletes
    # so many and no more, so when it stopped with more to go, the next is not
    # an hour away. And its survey does not count the receipt tables row by row.
    ledger, backend = _ledger()
    backend.purge_answers = [(PURGE_BATCHES, PURGE_BATCHES)] * shared_ledger._UPKEEP_ROUNDS
    await _run_upkeep(ledger)
    assert len(backend.purges) == shared_ledger._UPKEEP_ROUNDS
    due_in = ledger._upkeep_at - time.monotonic()
    assert 0 < due_in <= shared_ledger._UPKEEP_CATCH_UP_SECONDS
    assert ledger.stats()["receipts_purged"] == PURGE_BATCHES * shared_ledger._UPKEEP_ROUNDS
    assert backend.summaries == [False]  # estimated counts

    await _run_upkeep(ledger)  # this one finds little left: back to the hourly pace
    assert ledger._upkeep_at - time.monotonic() > shared_ledger._UPKEEP_CATCH_UP_SECONDS


async def test_the_first_upkeep_falls_at_a_random_point_within_the_hour():
    starts = [SharedSpendLedger(FakeSharedBackend(), instance_id=f"i{n}") for n in range(20)]
    offsets = [ledger._upkeep_at - ledger._started for ledger in starts]
    assert all(0 <= offset <= shared_ledger.UPKEEP_SECONDS for offset in offsets)
    assert len({round(offset) for offset in offsets}) > 1


def _row(instance: str, owner: str, *, n: int, amount: str, age: float, state: str = "active"):
    return {
        "instance_id": instance,
        "state": state,
        "n": n,
        "amount_usd": Decimal(amount),
        "oldest_age_seconds": age,
        "owner": owner,
    }


async def test_reservations_held_by_a_process_no_longer_heard_from_are_alerted(alerts, caplog):
    ledger, backend = _ledger()
    backend.open_rows = [
        _row("inst-dead", "stale", n=2, amount="0.60", age=1200.0),
        _row("inst-closed", "closed", n=1, amount="0.10", age=30.0),
        _row("inst-unit", "running", n=1, amount="0.30", age=5000.0),  # alive: not stranded
        _row("inst-dead", "stale", n=1, amount="0.50", age=9000.0, state="quarantined"),
    ]
    await _run_upkeep(ledger)

    assert _keys(alerts) == ["spend_reservations_stranded"]
    context = alerts[0][1]
    assert context["reservations"] == 3 and context["amount_held_usd"] == "0.70"
    assert context["instances"] == "inst-closed, inst-dead"
    database = ledger.stats()["database"]
    assert database["stranded"]["instances"] == ["inst-closed", "inst-dead"]
    assert database["active"]["reservations"] == 4
    assert database["quarantined"]["amount_usd"] == Decimal("0.50")
    assert database["age_seconds"] >= 0
    stranded_lines = [
        r for r in caplog.records if getattr(r, "event", None) == "spend_reservations_stranded"
    ]
    assert stranded_lines[0].instances == ["inst-closed", "inst-dead"]


async def test_a_reservation_whose_owner_missed_a_heartbeat_is_not_alerted_at_once(alerts):
    ledger, backend = _ledger()
    backend.open_rows = [_row("inst-slow", "stale", n=1, amount="0.30", age=120.0)]
    await _run_upkeep(ledger)
    assert _keys(alerts) == []
    assert ledger.stats()["database"]["stranded"]["reservations"] == 1  # seen, not paged yet


async def test_an_upkeep_that_fails_is_logged_and_leaves_the_ledger_working(alerts):
    ledger, backend = _ledger()
    backend.fail_summary = ConnectionRefusedError("the database is down")
    await _run_upkeep(ledger)
    assert ledger.stats()["database"] is None and _keys(alerts) == []
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    reservation.release()
    assert await ledger.flush(5)


# -- how an owner is known --------------------------------------------------------


def _instance(lifecycle: str = "running", *, stale: bool = False) -> dict[str, Any]:
    return {"lifecycle": lifecycle, "stale": stale}


@pytest.mark.parametrize(
    ("instance", "closed_by", "state"),
    [
        (None, None, "not registered"),
        (_instance(), None, "running"),
        (_instance("stopping"), None, "stopping"),
        (_instance("stopped"), None, "stopped"),
        (_instance(stale=True), None, "stale"),
        # It said so itself: closed, whatever the registry still shows.
        (_instance(), "shutdown", "closed"),
        (None, "shutdown", "closed"),
        # An operator said so: closed while the registry does not hear from it...
        (None, "operator", "closed"),
        (_instance(stale=True), "operator", "closed"),
        (_instance("stopped"), "operator", "closed"),
        # ...and alive when it does: the operator was wrong, the process settles its own.
        (_instance(), "operator", "running"),
    ],
)
def test_an_owner_is_known_by_its_closure_or_by_what_the_registry_says(instance, closed_by, state):
    assert owner_state(instance, closed_by) == state


def test_the_survey_adds_up_what_every_process_holds_and_what_is_stranded():
    rows = [
        _row("a", "running", n=3, amount="0.90", age=12.0),
        _row("b", "not registered", n=2, amount="0.40", age=700.0),
        _row("b", "not registered", n=1, amount="5.00", age=90000.0, state="quarantined"),
    ]
    found = survey({"reservations": rows, "receipts": {"batches": 7, "members": 9}})
    assert found["active"] == {
        "reservations": 5,
        "amount_usd": Decimal("1.30"),
        "oldest_age_seconds": 700.0,
    }
    assert found["quarantined"]["reservations"] == 1
    assert found["stranded"] == stranded(rows)
    assert found["stranded"] == {
        "reservations": 2,
        "amount_usd": Decimal("0.40"),
        "oldest_age_seconds": 700.0,
        "instances": ["b"],
    }
    assert found["receipts"] == {"batches": 7, "members": 9}
    assert survey({"reservations": []})["stranded"]["oldest_age_seconds"] is None


# -- evidence in the logs, and on the request log ----------------------------------


async def test_the_fields_of_a_refused_settlement_reach_the_formatted_log_line(caplog):
    # The alert says "the log lists each entry to reconcile": the formatters
    # emit only the fields they are told of, so these have to be among them.
    from serving.utils.logging import JsonFormatter

    ledger, backend = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        ledger.record(USER, 0.50)
    backend.fail_settle = asyncpg.exceptions.NumericValueOutOfRangeError("numeric field overflow")
    reservation.release()
    assert await ledger.flush(5)

    record = next(
        r for r in caplog.records if getattr(r, "event", None) == "spend_settlement_refused"
    )
    line = json.loads(JsonFormatter().format(record))
    assert line["reservations"] == [reservation.reservation_id]
    assert line["sqlstate"] == "22003" and line["batch_id"]
    assert line["increments"] == []


async def test_a_request_log_row_names_the_reservation_its_request_ran_under():
    from serving.servers.routers.completions_logging import CompletionsLogger

    class _LogStore:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        async def log_request(self, **row: Any) -> None:
            self.rows.append(row)

    store = _LogStore()
    logger = CompletionsLogger(log_store=store)
    ledger, _ = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    row = {"request_id": "req_1", "status_code": 200, "metadata": {"user_id": USER}}
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        # The Anthropic surface queues its row and then, without yielding,
        # records the charge: the row is written after both, and carries it.
        logger.schedule_log("req_1", row)
        ledger.record(USER, 0.12)
    logger.schedule_log("req_2", {**row, "request_id": "req_2"})  # no reservation: as before
    await logger.stop()

    by_id = {stored["request_id"]: stored for stored in store.rows}
    assert by_id["req_1"]["metadata"] == {
        "user_id": USER,
        "spend_reservation": reservation.reservation_id,
        "spend_charged_usd": "0.120000",
        # And the day the charge was recorded on, which its settlement counts it on.
        "spend_charged_days": [{"day": spend_ledger.utc_day(), "usd": "0.120000", "requests": 1}],
    }
    assert by_id["req_2"]["metadata"] == {"user_id": USER}
    assert row["metadata"] == {"user_id": USER}  # the caller's dict is not modified


async def test_a_row_for_a_request_that_was_charged_nothing_says_so():
    from serving.servers.routers.completions_logging import _with_spend_reservation

    ledger, _ = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    row = {"request_id": "req_1", "status_code": 502}
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        assert _with_spend_reservation(row)["metadata"] == {
            "spend_reservation": reservation.reservation_id,
            "spend_charged_usd": "0",
            "spend_charged_days": [],
        }
    assert _with_spend_reservation(row) is row  # outside a request with a reservation


async def test_a_row_says_on_which_day_each_part_of_its_charge_was_recorded(monkeypatch):
    # A request billed on both sides of midnight: its settlement counts each
    # part on its own day, and the row written afterwards says which.
    from serving.servers.routers.completions_logging import _with_spend_reservation

    ledger, _ = _ledger()
    reservation, _ = await ledger.admit(_check(ledger), 0.30)
    with req_ctx.push(**{req_ctx.SPEND_RESERVATION: reservation}):
        monkeypatch.setattr(spend_ledger, "utc_day", lambda now=None: "2031-03-31")
        ledger.record(USER, 0.10)
        ledger.record(USER, 0.02)
        monkeypatch.setattr(spend_ledger, "utc_day", lambda now=None: "2031-04-01")
        ledger.record(USER, 0.05)
        metadata = _with_spend_reservation({"request_id": "req_1"})["metadata"]
    assert reservation.charged_by_day() == [
        ("2031-03-31", Decimal("0.120000"), 2),
        ("2031-04-01", Decimal("0.050000"), 1),
    ]
    assert metadata["spend_charged_usd"] == "0.170000"
    assert metadata["spend_charged_days"] == [
        {"day": "2031-03-31", "usd": "0.120000", "requests": 2},
        {"day": "2031-04-01", "usd": "0.050000", "requests": 1},
    ]
    json.dumps(metadata)  # what the log store does with it
