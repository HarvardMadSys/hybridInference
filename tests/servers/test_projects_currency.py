"""Projects on a CNY ledger: native budgets in, native budgets and spend out.

The USD shape of the projects API is ``test_projects_api.py``'s; here the
deployment keeps its ledger in CNY at a fixed rate, and a project's budgets are
stored, validated and reported in it, with truthful USD equivalents beside.
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
from serving.servers.deps import (
    get_current_user,
    get_db_logger,
    get_embedding_adapters,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.routers import projects

POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-11-05", "synthetic fixed rate")

_ROW = {
    "id": "proj_eval",
    "name": "Eval",
    "description": None,
    "is_default": False,
    "budget_daily_usd": Decimal("2.9771"),
    "budget_daily_amount": Decimal("20"),
    "budget_monthly_usd": None,
    "budget_monthly_amount": None,
    "accounting_currency": "CNY",
    "allowed_models": None,
    "max_concurrent_requests": None,
    "created_at": datetime(2026, 11, 1, tzinfo=UTC),
}


@pytest.fixture(autouse=True)
def cny(monkeypatch):
    monkeypatch.setattr(accounting, "_policy", POLICY)


@pytest.fixture
def op_store():
    store = MagicMock()
    store.list_projects = AsyncMock(return_value=[dict(_ROW)])
    store.get_project = AsyncMock(return_value=dict(_ROW))
    store.create_project = AsyncMock(return_value=dict(_ROW))
    store.update_project = AsyncMock(return_value=dict(_ROW))
    store.get_project_spend = AsyncMock(return_value=(1.5, 1.5))
    store.get_project_spend_amount = AsyncMock(return_value=(Decimal("10.077"), Decimal("10.077")))
    store.count_project_keys = AsyncMock(return_value=1)
    # The USD ceilings must not be consulted on a CNY ledger.
    store.get_account_budgets = AsyncMock(side_effect=AssertionError("USD ceiling read"))
    store.get_account_budget_amounts = AsyncMock(return_value=(Decimal("67.18"), None))
    return store


@pytest.fixture
async def client(op_store):
    app = FastAPI()
    app.include_router(projects.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "user-1"}
    app.dependency_overrides[get_operational_store] = lambda: op_store
    app.dependency_overrides[get_db_logger] = lambda: MagicMock(log_admin_action=AsyncMock())
    app.dependency_overrides[get_router] = lambda: None
    app.dependency_overrides[get_embedding_adapters] = lambda: {}
    app.dependency_overrides[get_model_visibility_resolver] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.mark.asyncio
async def test_a_project_reports_its_native_budget_and_spend(client):
    (body,) = (await client.get("/user/projects")).json()
    assert body["currency"] == "CNY"
    assert Decimal(body["budget_daily"]) == Decimal("20")
    assert body["budget_monthly"] is None
    assert Decimal(body["spent_today"]) == Decimal("10.077")
    # The USD fields keep their USD meaning.
    assert body["budget_daily_usd"] == pytest.approx(2.9771)
    assert body["spent_today_usd"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_a_native_budget_is_stored_exactly_with_its_usd_equivalent(client, op_store):
    response = await client.post(
        "/user/projects", json={"name": "Fresh", "budget_daily": "20", "currency": "CNY"}
    )
    assert response.status_code == 201, response.text
    kwargs = op_store.create_project.await_args.kwargs
    assert kwargs["budget_daily_amount"] == Decimal("20")
    assert kwargs["budget_daily_usd"] == Decimal("2.9771")
    assert "budget_monthly_amount" not in kwargs


@pytest.mark.asyncio
async def test_a_native_budget_above_the_native_ceiling_is_refused(client, op_store):
    response = await client.post(
        "/user/projects", json={"name": "Fresh", "budget_daily": "67.19", "currency": "CNY"}
    )
    assert response.status_code == 422
    assert "67.18 CNY" in response.json()["detail"]
    op_store.create_project.assert_not_awaited()
    # Exactly the ceiling fits: compared natively, never through rounded USD.
    response = await client.post(
        "/user/projects", json={"name": "Fresh", "budget_daily": "67.18", "currency": "CNY"}
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_a_legacy_usd_budget_is_judged_at_the_fixed_rate(client, op_store):
    # 10.0002 USD is 67.1813 CNY, over the 67.18 CNY ceiling.
    response = await client.post(
        "/user/projects", json={"name": "Fresh", "budget_daily_usd": "10.0002"}
    )
    assert response.status_code == 422
    response = await client.post("/user/projects", json={"name": "Fresh", "budget_daily_usd": "10"})
    assert response.status_code == 201, response.text
    assert "budget_daily_amount" not in op_store.create_project.await_args.kwargs


@pytest.mark.asyncio
async def test_a_stale_page_or_both_units_are_refused(client, op_store):
    stale = await client.post(
        "/user/projects", json={"name": "Fresh", "budget_daily": "5", "currency": "USD"}
    )
    both = await client.post(
        "/user/projects",
        json={"name": "Fresh", "budget_daily": "5", "budget_daily_usd": "1", "currency": "CNY"},
    )
    assert stale.status_code == both.status_code == 422
    op_store.create_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_update_sends_the_native_budget_to_the_store(client, op_store):
    response = await client.patch(
        "/user/projects/proj_eval", json={"budget_monthly": "300.5", "currency": "CNY"}
    )
    assert response.status_code == 200, response.text
    _user, _project, changes = op_store.update_project.await_args.args
    assert changes["budget_monthly_amount"] == Decimal("300.5")
    assert changes["budget_monthly_usd"] == Decimal("44.7306")
