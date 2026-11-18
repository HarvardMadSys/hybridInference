"""The admin read of an account's projects and keys, on a CNY ledger.

It reuses the console's project and key responses, so an administrator sees
the native budgets and spend with their ``currency``, exactly as the owner
does, beside the USD fields that keep their USD meaning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving import accounting
from serving.accounting import AccountingPolicy
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.servers.routers.admin import user_projects

POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-10-23", "https://example.test/rate")
_CREATED = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def cny_policy(monkeypatch: pytest.MonkeyPatch) -> AccountingPolicy:
    monkeypatch.setattr(accounting, "_policy", POLICY)
    return POLICY


@pytest.fixture
def op_store():
    store = MagicMock()
    store.get_user_by_id = AsyncMock(return_value={"id": "user-1"})
    store.list_projects = AsyncMock(
        return_value=[
            {
                "id": "proj_research",
                "name": "Research",
                "is_default": False,
                "budget_daily_usd": Decimal("2.9771"),
                "budget_daily_amount": Decimal("20"),
                "budget_monthly_usd": None,
                "budget_monthly_amount": None,
                "accounting_currency": "CNY",
                "allowed_models": None,
                "max_concurrent_requests": None,
                "created_at": _CREATED,
            }
        ]
    )
    store.get_project_spend = AsyncMock(return_value=(1.5, 1.5))
    store.get_project_spend_amount = AsyncMock(return_value=(Decimal("10.077"), Decimal("10.077")))
    store.count_project_keys = AsyncMock(return_value=1)
    store.list_account_keys = AsyncMock(
        return_value=[
            {
                "key_prefix": "hyi-aaaaaaaa",
                "name": "runner",
                "created_at": _CREATED,
                "last_used_at": None,
                "status": "active",
                "spent_today_usd": 0.5,
                "spent_month_usd": 0.5,
                "spent_today_amount": Decimal("3.359000000123"),
                "spent_month_amount": Decimal("3.359000000456"),
                "project_id": "proj_research",
                "project_name": "Research",
                "budget_daily_usd": Decimal("1.4886"),
                "budget_daily_amount": Decimal("10"),
                "budget_monthly_usd": None,
                "budget_monthly_amount": None,
                "accounting_currency": "CNY",
                "allowed_models": None,
            }
        ]
    )
    return store


@pytest.mark.asyncio
async def test_an_administrator_reads_native_project_and_key_amounts(op_store):
    app = FastAPI()
    app.include_router(user_projects.router)
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_operational_store] = lambda: op_store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/admin/users/user-1/projects")

    assert response.status_code == 200
    [project] = response.json()["projects"]
    assert project["currency"] == "CNY"
    # Native amounts compared as the decimals they stand for.
    assert (Decimal(str(project["budget_daily"])), Decimal(str(project["spent_today"]))) == (
        Decimal("20"),
        Decimal("10.077"),
    )
    assert (project["budget_daily_usd"], project["spent_today_usd"]) == (2.9771, 1.5)
    # The key's native spend is its own counter's, not its USD value at the rate.
    [key] = response.json()["keys"]
    assert key["currency"] == "CNY"
    assert [Decimal(str(key[f])) for f in ("budget_daily", "spent_today", "spent_month")] == [
        Decimal("10"),
        Decimal("3.359000000123"),
        Decimal("3.359000000456"),
    ]
    assert (key["budget_daily_usd"], key["spent_today_usd"]) == (1.4886, 0.5)
