"""The spend ledger: in-flight reservations, and costs not yet in the counters.

Two properties carry the quota's correctness:

- requests admitted at the same time cannot all fit into one remaining budget
  (each reserves its estimate atomically with its check, and gives it back when
  it ends), and
- a cost recorded but not yet written to the counters -- the writer coalesces
  them -- still counts against the quota, until its write is confirmed and
  for any gate whose read may predate that.

And the writes stay exact: every recorded cost is written once, never lost to
coalescing, a failure or a shutdown, never applied twice.
"""

from __future__ import annotations

import asyncio
import time
import types
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from serving import spend_ledger
from serving.quota import ESTIMATED_REQUEST_COST_USD
from serving.spend_ledger import (
    LANDED_WINDOW_SECONDS,
    SpendCheck,
    SpendLedger,
    counted_amount,
    flush_all,
    ledger_for,
    utc_day,
)
from serving.storage.base import CostIncrement, CostWriteOutcomeUnknown

pytestmark = pytest.mark.asyncio


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    """The ledger's clock only: the event loop keeps the real one."""
    clock = _Clock()
    monkeypatch.setattr(spend_ledger, "time", types.SimpleNamespace(monotonic=clock))
    return clock


def _check(ledger: SpendLedger, **overrides: Any) -> SpendCheck:
    fields: dict[str, Any] = {
        "ledger": ledger,
        "user_id": "u1",
        "key_id": None,
        "read_at": time.monotonic(),
        "account_quota_usd": 1.0,
        "account_spent_usd": 0.0,
    }
    fields.update(overrides)
    return SpendCheck(**fields)


class _AtomicStore:
    """A store that applies batches atomically, like the PostgreSQL one."""

    atomic_cost_increments = True

    def __init__(self) -> None:
        self.batches: list[list[CostIncrement]] = []
        self.fail_next: list[BaseException] = []
        self.gate: asyncio.Event | None = None
        self.in_flight = 0
        self.max_in_flight = 0

    async def increment_user_costs(self, increments: list[CostIncrement]) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.fail_next:
                raise self.fail_next.pop(0)
            self.batches.append(list(increments))
        finally:
            self.in_flight -= 1

    def totals(self) -> dict[tuple[str, int | None, str], tuple[Decimal, int]]:
        out: dict[tuple[str, int | None, str], tuple[Decimal, int]] = {}
        for batch in self.batches:
            for inc in batch:
                key = (inc.user_id, inc.api_key_id, inc.day)
                cost, requests = out.get(key, (Decimal(0), 0))
                out[key] = (cost + Decimal(inc.cost_usd), requests + inc.requests)
        return out


# -- reservations ----------------------------------------------------------------


async def test_concurrent_admissions_cannot_share_one_remaining_budget():
    ledger = SpendLedger(object())
    # $0.055 left: the estimate is $0.01, so five fit and the sixth does not.
    checks = [_check(ledger, account_quota_usd=0.055) for _ in range(6)]

    outcomes = [check.admit() for check in checks]

    admitted = [reservation for reservation, refusal in outcomes if reservation]
    refused = [refusal for reservation, refusal in outcomes if refusal]
    assert len(admitted) == 5
    assert len(refused) == 1
    assert (refused[0].scope, refused[0].window) == ("account", "day")
    assert refused[0].spent_usd == pytest.approx(5 * ESTIMATED_REQUEST_COST_USD)


async def test_a_released_reservation_frees_its_share_once():
    ledger = SpendLedger(object())
    first, _ = _check(ledger, account_quota_usd=0.015).admit()
    assert first is not None
    assert _check(ledger, account_quota_usd=0.015).refusal() is not None

    first.release()
    first.release()  # idempotent: does not free someone else's share

    assert _check(ledger, account_quota_usd=0.015).refusal() is None
    assert ledger._reserved_users == {}


async def test_a_lone_request_is_answered_exactly_as_before():
    """Nothing in flight, nothing unwritten: the gate's own arithmetic, unchanged."""
    ledger = SpendLedger(object())

    assert _check(ledger, account_quota_usd=1.0, account_spent_usd=0.99).refusal() is None
    refusal = _check(ledger, account_quota_usd=1.0, account_spent_usd=0.995).refusal()
    assert refusal is not None
    assert refusal.spent_usd == pytest.approx(0.995)


async def test_reservations_count_against_the_key_budget_and_the_month():
    ledger = SpendLedger(object())
    held, _ = _check(ledger, key_id=7).admit()
    assert held is not None

    same_key = _check(ledger, key_id=7, key_budget_daily_usd=0.015)
    other_key = _check(ledger, key_id=8, key_budget_daily_usd=0.015)
    month = _check(ledger, account_quota_monthly_usd=0.015)

    assert same_key.refusal().scope == "key"
    assert other_key.refusal() is None or other_key.refusal().scope != "key"
    assert month.refusal().window == "month"


async def test_a_check_without_a_ledger_only_checks():
    check = _check(None, account_quota_usd=0.005)

    reservation, refusal = check.admit()

    assert reservation is None
    assert refusal is not None


async def test_a_reservation_is_its_estimate_up_to_what_the_tightest_limit_has_left():
    """Refused only when the minimum does not fit; otherwise it holds what it may cost, or the rest."""
    ledger = SpendLedger(object())

    plenty, _ = _check(ledger, account_quota_usd=100.0).admit(2.5)
    near, refusal = _check(
        ledger, user_id="u2", account_quota_usd=1.0, account_spent_usd=0.96
    ).admit(2.5)
    key, _ = _check(
        ledger, user_id="u3", key_id=9, key_budget_daily_usd=0.5, account_quota_usd=100.0
    ).admit(2.5)

    assert plenty.amount == pytest.approx(2.5)
    assert refusal is None
    assert near.amount == pytest.approx(0.04)
    assert key.amount == pytest.approx(0.5)
    # Whatever is left is held: the next request, however small, waits.
    assert _check(ledger, user_id="u2", account_quota_usd=1.0, account_spent_usd=0.96).admit()[1]
    near.release()
    assert ledger._reserved_users.get("u2") is None


async def test_the_default_reservation_is_the_flat_minimum():
    """What the batch worker reserves for an item, unchanged."""
    ledger = SpendLedger(object())

    reservation, _ = _check(ledger, account_quota_usd=0.015).admit()

    assert reservation.amount == ESTIMATED_REQUEST_COST_USD


# -- costs recorded, not yet written ------------------------------------------------


async def test_an_unwritten_cost_counts_until_its_write_is_confirmed():
    store = _AtomicStore()
    store.gate = asyncio.Event()
    ledger = SpendLedger(store)

    ledger.record("u1", 0.5, api_key_id=7)
    await asyncio.sleep(0)  # the write is in flight, blocked

    check = _check(ledger, account_quota_usd=1.0, account_spent_usd=0.495)
    assert check.refusal() is not None  # 0.495 + 0.5 unwritten + 0.01 > 1.0
    key_check = _check(ledger, key_id=7, key_budget_daily_usd=0.5, account_quota_usd=100.0)
    assert key_check.refusal().scope == "key"

    store.gate.set()
    assert await ledger.flush()
    # A gate that reads the counters now sees the cost there instead.
    later = _check(ledger, account_quota_usd=1.0, account_spent_usd=0.995 - 0.5)
    assert later.refusal() is None
    assert ledger._unconfirmed == {}


async def test_a_write_confirmed_after_a_gate_began_reading_still_counts_for_it(clock):
    """The gate's read may predate the write: counted again, never missed."""
    store = _AtomicStore()
    ledger = SpendLedger(store)
    read_before = _check(ledger, account_quota_usd=0.505, read_at=clock.now)

    clock.now += 1
    ledger.record("u1", 0.5)
    assert await ledger.flush()

    assert read_before.refusal() is not None  # 0 read + 0.5 confirmed after + 0.01
    clock.now += 1
    read_after = _check(ledger, account_quota_usd=0.505, read_at=clock.now)
    assert read_after.refusal() is None  # the 0.5 is in what this one read


async def test_confirmed_writes_are_kept_only_for_the_window(clock):
    store = _AtomicStore()
    ledger = SpendLedger(store)
    ledger.record("u1", 0.5)
    assert await ledger.flush()
    assert len(ledger._landed) == 1

    clock.now += LANDED_WINDOW_SECONDS + 1
    _check(ledger).refusal()

    assert len(ledger._landed) == 0


async def test_a_gate_older_than_the_window_counts_every_write_still_kept(clock):
    store = _AtomicStore()
    ledger = SpendLedger(store)
    stale_read_at = clock.now
    clock.now += LANDED_WINDOW_SECONDS + 10
    ledger.record("u1", 0.5)
    assert await ledger.flush()
    clock.now += 5

    stale = _check(ledger, account_quota_usd=0.505, read_at=stale_read_at)

    assert stale.refusal() is not None


async def test_another_days_cost_counts_for_the_month_only(monkeypatch):
    ledger = SpendLedger(_AtomicStore())
    day = utc_day()
    other_day = day[:8] + ("02" if day[8:] != "02" else "03")
    ledger._unconfirmed["u1"] = {(None, other_day): spend_ledger._Unconfirmed(0.5, 1)}

    assert _check(ledger, account_quota_usd=0.1).refusal() is None
    month = _check(ledger, account_quota_usd=10.0, account_quota_monthly_usd=0.1)
    assert month.refusal().window == "month"


# -- the writer --------------------------------------------------------------------


async def test_costs_recorded_during_a_write_go_out_coalesced_in_the_next():
    store = _AtomicStore()
    store.gate = asyncio.Event()
    ledger = SpendLedger(store)
    costs = [0.0000123456, 0.0000025, 0.25, 0.0004999, 0.1]

    ledger.record("u1", costs[0], api_key_id=7)
    await asyncio.sleep(0)  # first write in flight with one cost
    for cost in costs[1:]:
        ledger.record("u1", cost, api_key_id=7)
    ledger.record("u2", 0.3)
    store.gate.set()
    assert await ledger.flush()

    assert len(store.batches) == 2
    assert store.max_in_flight == 1
    coalesced = {(inc.user_id, inc.api_key_id): inc for inc in store.batches[1]}
    assert coalesced[("u1", 7)].requests == len(costs) - 1
    # Exactly what the separate increments would have added, each rounded
    # to the counter's six places on its own.
    assert store.totals()[("u1", 7, utc_day())] == (
        sum(counted_amount(cost) for cost in costs),
        len(costs),
    )
    assert store.totals()[("u2", None, utc_day())] == (Decimal("0.300000"), 1)


async def test_a_burst_needs_one_connection_at_a_time():
    store = _AtomicStore()
    store.gate = asyncio.Event()
    ledger = SpendLedger(store)
    ledger.record("user-0", 0.001)
    await asyncio.sleep(0)  # that one's write is in flight, blocked

    for i in range(999):
        ledger.record(f"user-{i % 10}", 0.001)
    await asyncio.sleep(0)
    assert store.in_flight == 1
    assert len(ledger._pending) == 10  # one entry per user, key and day

    store.gate.set()
    assert await ledger.flush()
    assert store.max_in_flight == 1
    assert sum(r for _, r in store.totals().values()) == 1000


async def test_a_failed_write_is_retried_and_counted_meanwhile():
    store = _AtomicStore()
    store.fail_next = [ConnectionError("db down")]
    ledger = SpendLedger(store)

    task = ledger.record("u1", 0.5)
    await asyncio.wait({task})

    assert store.batches == []
    assert ledger._retry is not None  # backing off, not hammering
    assert _check(ledger, account_quota_usd=0.505).refusal() is not None
    assert await ledger.flush()
    assert store.totals() == {("u1", None, utc_day()): (Decimal("0.500000"), 1)}


async def test_an_unknown_outcome_is_not_written_again_but_still_counted(caplog):
    store = _AtomicStore()
    store.fail_next = [CostWriteOutcomeUnknown(42)]
    ledger = SpendLedger(store)

    await asyncio.wait({ledger.record("u1", 0.5)})

    assert await ledger.flush()
    assert store.batches == []  # never written twice
    assert _check(ledger, account_quota_usd=0.505).refusal() is not None
    assert ledger.unwritten() == (pytest.approx(0.5), 1)
    assert any(getattr(r, "xid", None) == 42 for r in caplog.records)


async def test_a_store_without_atomic_batches_is_written_entry_by_entry():
    """A partial failure retries only what did not land: nothing counted twice."""
    store = MagicMock()
    calls: list[tuple] = []
    failures = {"u2": 1}

    async def increment(user_id, cost, *, day, api_key_id, requests):
        if failures.get(user_id):
            failures[user_id] -= 1
            raise RuntimeError("transient")
        calls.append((user_id, cost, day, api_key_id, requests))

    store.increment_user_cost = AsyncMock(side_effect=increment)
    ledger = SpendLedger(store)
    ledger.record("u1", 0.25, api_key_id=3)
    ledger.record("u2", 0.5)

    assert await ledger.flush()

    assert sorted((c[0], c[1], c[3], c[4]) for c in calls) == [
        ("u1", 0.25, 3, 1),
        ("u2", 0.5, None, 1),
    ]


async def test_flush_all_writes_what_is_pending_before_shutdown():
    store = _AtomicStore()
    store.gate = asyncio.Event()
    ledger = ledger_for(store)
    ledger.record("u1", 0.5)
    await asyncio.sleep(0)
    ledger.record("u1", 0.25)  # waiting behind the write in flight

    asyncio.get_running_loop().call_later(0.01, store.gate.set)
    await flush_all(timeout=5)

    assert store.totals() == {("u1", None, utc_day()): (Decimal("0.750000"), 2)}
    assert ledger.unwritten() == (0.0, 0)


async def test_flush_all_reports_what_it_could_not_write(caplog):
    store = _AtomicStore()
    store.fail_next = [ConnectionError("down")] * 100
    ledger = ledger_for(store)
    ledger.record("u1", 0.5)

    await flush_all(timeout=0.2)

    assert ledger.unwritten() == (pytest.approx(0.5), 1)
    assert any(
        getattr(r, "event", None) == "cost_counter_unwritten_at_shutdown" for r in caplog.records
    )


async def test_zero_and_unattributed_costs_are_not_recorded():
    ledger = SpendLedger(_AtomicStore())

    assert ledger.record("u1", 0.0) is None
    assert ledger.record("", 0.5) is None
    assert ledger.record("u1", float("nan")) is None
    assert ledger._pending == {}


# -- an entry the counters refuse ------------------------------------------------

#: The most a ``DECIMAL(12, 6)`` counter holds.
_COUNTER_MAX = Decimal("999999.999999")


class _Counters:
    """An atomic store that behaves like PostgreSQL's ``DECIMAL(12, 6)`` counters.

    The batch is one statement: when any counter would overflow, all of it
    fails, with the error asyncpg raises for that (SQLSTATE 22003).
    """

    atomic_cost_increments = True

    def __init__(self) -> None:
        self.counters: dict[tuple[str, str], Decimal] = {}
        self.calls = 0
        self.down = 0

    async def increment_user_costs(self, increments: list[CostIncrement]) -> None:
        self.calls += 1
        if self.down:
            self.down -= 1
            raise ConnectionRefusedError("database restarting")
        staged = dict(self.counters)
        for inc in increments:
            key = (inc.user_id, inc.day)
            staged[key] = staged.get(key, Decimal(0)) + Decimal(inc.cost_usd)
            if staged[key] > _COUNTER_MAX:
                raise asyncpg.exceptions.NumericValueOutOfRangeError("numeric field overflow")
        self.counters = staged


@pytest.fixture
def alerts(monkeypatch) -> list[tuple[str, dict[str, Any], str]]:
    """The alerts the ledger sends, as (title, context, dedupe key)."""
    sent: list[tuple[str, dict[str, Any], str]] = []

    def alert_slack(severity, title, context, *, dedupe_key, cooldown_sec):
        sent.append((title, context, dedupe_key))

    module = types.SimpleNamespace(
        AlertSeverity=types.SimpleNamespace(ERROR="error"), alert_slack=alert_slack
    )
    monkeypatch.setattr(spend_ledger, "_send_alert", lambda send: send(module))
    return sent


async def test_an_entry_the_counters_refuse_does_not_hold_back_anyone_elses(caplog, alerts):
    store = _Counters()
    # This account's counter is all but full: its next cost overflows it.
    store.counters[("full", utc_day())] = _COUNTER_MAX - Decimal("0.5")
    ledger = SpendLedger(store)

    ledger.record("full", 0.75)
    for index in range(50):
        ledger.record(f"user-{index}", 0.01)
    assert await ledger.flush(timeout=5)

    written = {user for user, _ in store.counters if user.startswith("user-")}
    assert len(written) == 50
    # Set aside, not written again and again with everyone else's.
    assert ledger._pending == {}
    assert ledger._retry is None
    assert ledger.unwritten() == (pytest.approx(0.75), 1)
    # Still counted against its quota in this process.
    assert _check(ledger, user_id="full", account_quota_usd=0.755).refusal() is not None
    refused = [
        r for r in caplog.records if getattr(r, "event", None) == "cost_counter_write_refused"
    ]
    assert [entry["user_id"] for entry in refused[0].entries] == ["full"]
    assert refused[0].entries[0]["sqlstate"] == "22003"
    assert [key for _, _, key in alerts] == ["cost_counter_write_refused"]


async def test_one_refused_entry_on_its_own_is_set_aside_at_once(alerts):
    store = _Counters()
    store.counters[("full", utc_day())] = _COUNTER_MAX
    ledger = SpendLedger(store)

    await asyncio.wait({ledger.record("full", 0.25)})

    assert store.calls == 1
    assert ledger._pending == {}
    assert ledger._retry is None
    assert ledger.unwritten() == (pytest.approx(0.25), 1)
    assert len(alerts) == 1


async def test_a_database_outage_is_retried_whole_not_entry_by_entry(alerts):
    store = _Counters()
    store.down = 1
    ledger = SpendLedger(store)
    for index in range(20):
        ledger.record(f"user-{index}", 0.01)
    await asyncio.sleep(0)

    assert store.calls == 1  # one batch, no futile write per entry
    assert ledger._retry is not None
    assert await ledger.flush(timeout=5)
    assert len(store.counters) == 20
    assert alerts == []


async def test_an_unexplained_batch_failure_still_lets_the_others_through():
    """A store error that names no cause: the others go in, the entry is retried."""
    store = _AtomicStore()
    batches_seen: list[int] = []

    async def increment(increments: list[CostIncrement]) -> None:
        batches_seen.append(len(increments))
        if any(inc.user_id == "odd" for inc in increments):
            raise RuntimeError("something about this entry")
        store.batches.append(list(increments))

    store.increment_user_costs = increment  # type: ignore[method-assign]
    ledger = SpendLedger(store)
    ledger.record("odd", 0.5)
    for index in range(10):
        ledger.record(f"user-{index}", 0.01)

    assert not await ledger.flush(timeout=0.2)

    assert {inc.user_id for batch in store.batches for inc in batch} == {
        f"user-{index}" for index in range(10)
    }
    # Not refused for its values, as far as anyone can tell: kept for a retry.
    assert list(ledger._pending) == [("odd", None, utc_day())]
    assert ledger._refused == []


async def test_a_cost_beyond_any_real_request_is_recorded_as_the_ceiling(caplog, alerts):
    ledger = SpendLedger(_AtomicStore())

    ledger.record("u1", 2_000_000.0)
    ledger.record("u2", float("inf"))

    ceiling = spend_ledger.MAX_REQUEST_COST_USD
    day = utc_day()
    assert ledger._pending[("u1", None, day)].cost == ceiling
    assert ledger._pending[("u2", None, day)].counted == counted_amount(ceiling)
    assert [
        r.user_id
        for r in caplog.records
        if getattr(r, "event", None) == "cost_above_request_ceiling"
    ] == ["u1", "u2"]
    assert len(alerts) == 2
    assert await ledger.flush()


async def test_one_ledger_per_store():
    first, second = _AtomicStore(), _AtomicStore()

    assert ledger_for(first) is ledger_for(first)
    assert ledger_for(first) is not ledger_for(second)
    assert ledger_for(None) is None


async def test_the_ledger_pages_through_alert_slack_under_a_catalogued_type(monkeypatch):
    from serving.observability import alerts as alerts_module
    from serving.observability.alert_types import alert_type_of, get_alert_type

    alert_slack = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_module, "alert_slack", alert_slack)
    ledger = SpendLedger(_AtomicStore())

    ledger.record("u1", 2_000_000.0)
    await asyncio.gather(*spend_ledger._ALERTS)

    alert_slack.assert_awaited_once()
    key = alert_slack.await_args.kwargs["dedupe_key"]
    assert get_alert_type(alert_type_of(key)) is not None
    assert await ledger.flush()
