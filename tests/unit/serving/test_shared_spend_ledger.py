"""The shared ledger's own logic, on an in-memory backend: locks, meters, liabilities."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from serving.shared_spend import mode as spend_mode
from serving.shared_spend.ledger import SharedSpendLedger
from serving.shared_spend.store import CostLine, Member, SettlementBatch, usd
from serving.spend_ledger import SpendCheck, SpendLedger, ledger_for, utc_day
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
