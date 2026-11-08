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


async def test_one_ledger_per_store():
    first, second = _AtomicStore(), _AtomicStore()

    assert ledger_for(first) is ledger_for(first)
    assert ledger_for(first) is not ledger_for(second)
    assert ledger_for(None) is None
