"""``GET /v1/key`` on a CNY ledger: native limits and spends beside the USD fields."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving import accounting
from serving.accounting import AccountingPolicy
from serving.servers.auth import _user_context, verify_api_key_for_balance
from serving.servers.routers import models

POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-11-05", "synthetic fixed rate")


@pytest.fixture
def cny(monkeypatch):
    monkeypatch.setattr(accounting, "_policy", POLICY)


def _identity() -> dict:
    """An auth-lookup row as the CNY gate reads it: native columns and counters."""
    return {
        "id": 7,
        "user_id": "cny-user",
        "role": "pro",
        "key_prefix": "hyi-syn",
        "budget_daily_usd": Decimal("1.4886"),
        "budget_daily_amount": Decimal("10"),
        "key_spent_today_usd": Decimal("0.5"),
        "key_spent_today_amount": Decimal("3.359"),
        "project_id": "proj_eval",
        "project_name": "Eval",
        "project_budget_daily_usd": Decimal("2.9771"),
        "project_budget_daily_amount": Decimal("20"),
        "project_spent_today_usd": Decimal("1"),
        "project_spent_today_amount": Decimal("6.718"),
        "quota_monthly_cost_usd": None,
    }


async def _describe(context: dict) -> dict:
    app = FastAPI()
    app.include_router(models.router)
    app.dependency_overrides[verify_api_key_for_balance] = lambda: context
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return (await client.get("/v1/key")).json()


@pytest.mark.asyncio
async def test_native_budgets_are_reported_exactly_on_a_cny_ledger(cny):
    context = _user_context(
        _identity(),
        key_hash="h",
        api_key_id=7,
        quota_daily_cost_usd=10.0,
        spent_today_usd=1.0,
    )
    context.update(currency="CNY", quota_daily_cost=Decimal("67.18"), spent_today=Decimal("6.718"))
    body = await _describe(context)
    assert body["currency"] == "CNY"
    key_day = body["budgets"]["key"]["day"]
    assert (Decimal(str(key_day["limit"])), Decimal(str(key_day["spent"]))) == (
        Decimal("10"),
        Decimal("3.359"),
    )
    assert Decimal(str(key_day["remaining"])) == Decimal("6.641")
    assert key_day["limit_usd"] == pytest.approx(1.4886)
    project_day = body["budgets"]["project"]["day"]
    assert Decimal(str(project_day["limit"])) == Decimal("20")
    assert Decimal(str(project_day["remaining"])) == Decimal("13.282")
    account_day = body["budgets"]["account"]["day"]
    assert Decimal(str(account_day["limit"])) == Decimal("67.18")
    assert Decimal(str(account_day["spent"])) == Decimal("6.718")
    assert body["budgets"]["key"]["month"] is None


@pytest.mark.asyncio
async def test_a_usd_ledger_keeps_the_upstream_shape():
    context = _user_context(
        {**_identity(), "budget_daily_usd": 1.5, "key_spent_today_usd": 0.5},
        key_hash="h",
        api_key_id=7,
        quota_daily_cost_usd=10.0,
        spent_today_usd=1.0,
    )
    body = await _describe(context)
    assert "currency" not in body
    assert body["budgets"]["key"]["day"] == {
        "limit_usd": 1.5,
        "spent_usd": 0.5,
        "remaining_usd": 1.0,
    }
