"""The playground's budget gate counts in the ledger currency, as the API's does.

The console session is gated like ``X-On-Behalf-Of``: the account's quotas,
no key. Its reservation is the request's estimate in the ledger currency
(:func:`serving.quota.estimated_request_cost`), so the check it is reserved
against must be too: on a CNY ledger the native limits and counters, not their
USD equivalents.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from serving import accounting, quota
from serving.accounting import AccountingPolicy
from serving.spend_ledger import ledger_for
from tests.unit.servers.test_playground_access import (
    INTERNAL,
    _chat,
    _client,
    _Deployment,
    _Store,
)

POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-10-23", "https://example.test/rate")


@pytest.fixture(autouse=True)
def cny_policy(monkeypatch: pytest.MonkeyPatch) -> AccountingPolicy:
    monkeypatch.setattr(accounting, "_policy", POLICY)
    return POLICY


class _NativeStore(_Store):
    """The account's day in both units, as the triggers keep them."""

    def __init__(self, *, daily_quota: float, spent_today: Decimal) -> None:
        super().__init__(daily_quota=daily_quota, spent_today=float(POLICY.to_usd(spent_today)))
        self.spent_today_amount = spent_today

    async def get_user_cost_today_amount(self, user_id: str) -> Decimal:
        return self.spent_today_amount

    async def increment_user_costs(self, increments: list[Any]) -> None:
        self.increments.extend({"user_id": i.user_id} for i in increments)


async def _reserved(store: _Store, user_id: str) -> list[float]:
    return list(ledger_for(store)._reserved_users.get(user_id, ()))


@pytest.mark.asyncio
async def test_a_request_that_fits_the_native_budget_is_admitted() -> None:
    # 1 USD a day is 6.718 CNY; 6.3821 CNY spent leaves 0.3359 CNY, room for
    # the flat minimum (0.06718 CNY), compared in CNY.
    store = _NativeStore(daily_quota=1.0, spent_today=Decimal("6.3821"))
    assert store.spent_today_amount + quota.estimated_request_cost() <= POLICY.from_usd(1)
    async with _client(_Deployment(), INTERNAL, store=store) as client:
        response = await _chat(client, "open-model")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_a_request_over_the_native_budget_is_refused() -> None:
    deployment = _Deployment()
    store = _NativeStore(daily_quota=1.0, spent_today=Decimal("6.7"))
    async with _client(deployment, INTERNAL, store=store) as client:
        response = await _chat(client, "open-model")
    assert response.status_code == 429
    assert deployment.calls == 0
    assert await _reserved(store, INTERNAL["user_id"]) == []


@pytest.mark.asyncio
async def test_playground_preserves_exact_native_account_quota(monkeypatch) -> None:
    from serving.servers.routers import playground

    native = Decimal("6.718000000123")

    class PreciseStore(_NativeStore):
        async def get_quota_context_for_user(self, user_id):
            return [
                {
                    "quota_daily_cost_usd": Decimal("1.00000000"),
                    "quota_daily_cost_amount": native,
                    "quota_monthly_cost_usd": None,
                    "quota_monthly_cost_amount": None,
                }
            ]

    original_gate = playground._gate
    identities = []

    async def capture_gate(identity, **kwargs):
        identities.append(identity)
        return await original_gate(identity, **kwargs)

    monkeypatch.setattr(playground, "_gate", capture_gate)
    store = PreciseStore(daily_quota=1.0, spent_today=Decimal("6.3821"))
    async with _client(_Deployment(), INTERNAL, store=store) as client:
        response = await _chat(client, "open-model")
    assert response.status_code == 200
    assert identities[0]["quota_daily_cost_amount"] == native
    assert identities[0]["quota_monthly_cost_amount"] is None
