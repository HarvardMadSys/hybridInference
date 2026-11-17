"""The spend ledger on a non-USD ledger: native charges, native limits, exact writes.

The ledger counts in the deployment's accounting currency
(``docs/developer/accounting.md``): a CNY deployment records the native charge
its prices produce, reserves native estimates, compares native limits as exact
decimals and writes the native counters. A USD deployment keeps exactly the
upstream behaviour, which ``test_spend_ledger.py`` covers.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from serving import accounting, quota
from serving.accounting import AccountingPolicy
from serving.spend_ledger import SpendCheck, SpendLedger, counted_amount, utc_day

if TYPE_CHECKING:
    from serving.storage.base import CostIncrement

pytestmark = pytest.mark.asyncio

RATE = Decimal("6.7180")


@pytest.fixture
def cny(monkeypatch: pytest.MonkeyPatch) -> AccountingPolicy:
    policy = AccountingPolicy(
        currency="CNY",
        usd_to_currency_rate=RATE,
        rate_date="2026-11-05",
        rate_source="https://example.test/synthetic-fixed-rate",
    )
    monkeypatch.setattr(accounting, "_policy", policy)
    return policy


class _AtomicStore:
    atomic_cost_increments = True

    def __init__(self) -> None:
        self.batches: list[list[CostIncrement]] = []

    async def increment_user_costs(self, increments: list[CostIncrement]) -> None:
        self.batches.append(list(increments))


def _check(ledger: SpendLedger | None, **overrides: Any) -> SpendCheck:
    fields: dict[str, Any] = {
        "ledger": ledger,
        "user_id": "u1",
        "key_id": None,
        "read_at": time.monotonic(),
        "account_quota_usd": Decimal("67.18"),
        "account_spent_usd": Decimal(0),
    }
    fields.update(overrides)
    return SpendCheck(**fields)


async def test_a_native_charge_is_written_to_the_native_counters_exactly(cny):
    store = _AtomicStore()
    ledger = SpendLedger(store)
    ledger.record("u1", 0.0725, api_key_id=42)
    assert await ledger.flush()

    (increment,) = store.batches[0]
    assert increment.cost_amount == Decimal("0.072500000000")
    # The USD value is the equivalent of exactly that native amount.
    assert increment.cost_usd == Decimal("0.072500000000") / RATE
    assert (increment.user_id, increment.api_key_id, increment.day) == ("u1", 42, utc_day())


async def test_a_store_without_batches_gets_the_native_increment_not_the_usd_one(cny):
    class _Store:
        increment_user_cost = AsyncMock()
        increment_user_cost_amount = AsyncMock()

    store = _Store()
    ledger = SpendLedger(store)
    ledger.record("u1", 0.5, api_key_id=7)
    assert await ledger.flush()

    store.increment_user_cost.assert_not_awaited()
    store.increment_user_cost_amount.assert_awaited_once()
    args, kwargs = store.increment_user_cost_amount.await_args
    assert args == ("u1", Decimal("0.500000000000"))
    assert kwargs["api_key_id"] == 7
    assert kwargs["requests"] == 1


async def test_counted_amounts_round_to_the_native_column_scale(cny):
    # NUMERIC(24, 12) stores twelve places; a coalesced batch must add exactly
    # what separate increments would.
    assert counted_amount(1e-13) == Decimal(0)
    assert counted_amount(0.1234567890126) == Decimal("0.123456789013")


async def test_usd_deployments_round_to_the_usd_column_scale():
    assert accounting.get_accounting_policy().currency == "USD"
    assert counted_amount(0.1234567) == Decimal("0.123457")


async def test_the_flat_estimate_and_its_ceiling_are_native(cny):
    # Exact decimals: the USD constants at the fixed rate.
    assert quota.estimated_request_cost() == Decimal("0.06718")
    assert quota.max_reserved_request_cost() == Decimal("33.590")
    assert quota.estimate_request_cost(
        prompt_tokens=10, output_tokens=0, prompt_price=1.0, completion_price=1.0
    ) == Decimal("0.06718")
    assert quota.estimate_request_cost(
        prompt_tokens=10**9, output_tokens=0, prompt_price=1.0, completion_price=1.0
    ) == Decimal("33.590")


async def test_a_native_daily_quota_refuses_on_its_exact_amount(cny):
    ledger = SpendLedger(_AtomicStore())
    # 67.18 CNY limit, 67.11 spent: the 0.06718 CNY estimate still fits.
    assert _check(ledger, account_spent_usd=Decimal("67.11")).refusal() is None
    refusal = _check(ledger, account_spent_usd=Decimal("67.12")).refusal()
    assert refusal is not None
    assert (refusal.scope, refusal.window) == ("account", "day")
    assert refusal.limit_amount == Decimal("67.18")
    assert refusal.spent_amount == Decimal("67.12")
    # The USD fields keep their USD meaning.
    assert refusal.limit_usd == pytest.approx(10.0)


async def test_recorded_native_spend_counts_before_it_is_written(cny):
    store = _AtomicStore()  # the ledger holds its store weakly
    ledger = SpendLedger(store)
    check = _check(ledger, account_spent_usd=Decimal("60"))
    assert check.refusal() is None
    # Counted from when it is recorded, and after its write for a gate whose
    # read predates the write.
    ledger.record("u1", 7.15)
    refusal = check.refusal()
    assert refusal is not None
    assert refusal.spent_amount == Decimal("67.15")
    assert await ledger.flush()
    assert store.batches[0][0].cost_amount == Decimal("7.150000000000")
    assert check.refusal() is not None


async def test_a_project_budget_is_compared_natively_and_named(cny):
    check = _check(
        None,
        key_id=3,
        project_id="proj_a",
        project_name="Research",
        project_budget_daily_usd=Decimal("20"),
        project_spent_today_usd=Decimal("19.95"),
    )
    refusal = check.refusal()
    assert refusal is not None
    assert (refusal.scope, refusal.window, refusal.scope_name) == ("project", "day", "Research")
    assert refusal.limit_amount == Decimal("20")
    body, headers = quota.refusal_payload(refusal)
    assert body["code"] == "budget_exceeded"
    assert body["project"] == "Research"
    assert "¥20.00" in body["message"]
    assert body["quota"] == 20.0
    assert body["currency"] == "CNY"
    assert headers["X-Accounting-Currency"] == "CNY"
    assert headers["X-RateLimit-Cost-Currency"] == "USD"


async def test_a_key_budget_is_narrower_than_its_project(cny):
    check = _check(
        None,
        key_id=3,
        key_prefix="hyi-abc",
        key_label='"ci"',
        key_budget_daily_usd=Decimal("1"),
        key_spent_today_usd=Decimal("0.99"),
        project_id="proj_a",
        project_name="Research",
        project_budget_daily_usd=Decimal("0.5"),
        project_spent_today_usd=Decimal("0.49"),
    )
    refusal = check.refusal()
    assert refusal is not None
    assert refusal.scope == "key"


async def test_a_reservation_is_capped_by_the_native_headroom(cny):
    ledger = SpendLedger(_AtomicStore())
    check = _check(ledger, account_spent_usd=Decimal("66.18"))
    reservation, refusal = check.admit(20.0)
    assert refusal is None
    assert reservation is not None
    # 1 CNY left: the reservation holds what is left, never less than the minimum.
    assert reservation.amount == Decimal("1.00")
    reservation.release()
    minimum, refusal = check.admit()
    assert refusal is None
    assert minimum is not None
    assert minimum.amount == quota.estimated_request_cost()


async def test_a_reservation_never_falls_below_the_native_minimum(cny):
    # A caller passing the USD-sized flat estimate still reserves the native one.
    ledger = SpendLedger(_AtomicStore())
    reservation, refusal = _check(ledger).admit(quota.ESTIMATED_REQUEST_COST_USD)
    assert refusal is None
    assert reservation is not None
    assert reservation.amount == quota.estimated_request_cost()
