"""Admin read of one account's projects and keys."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import get_operational_store, verify_admin_access
from serving.servers.routers.admin import user_projects

_CREATED = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def op_store():
    store = MagicMock()
    store.get_user_by_id = AsyncMock(return_value={"id": "user-1"})
    store.list_projects = AsyncMock(
        return_value=[
            {
                "id": "proj_d_user-1",
                "name": "Default",
                "is_default": True,
                "budget_daily_usd": None,
                "budget_monthly_usd": None,
                "allowed_models": None,
                "max_concurrent_requests": None,
                "created_at": _CREATED,
            },
            {
                "id": "proj_ci",
                "name": "CI",
                "is_default": False,
                "budget_daily_usd": 5,
                "budget_monthly_usd": None,
                "allowed_models": ["m-1"],
                "max_concurrent_requests": 2,
                "created_at": _CREATED,
            },
        ]
    )
    store.get_project_spend = AsyncMock(return_value=(1.0, 4.0))
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
                "spent_month_usd": 2,
                "project_id": "proj_ci",
                "project_name": "CI",
                "budget_daily_usd": None,
                "budget_monthly_usd": None,
                "allowed_models": None,
                "api_key_encrypted": "ciphertext",
            }
        ]
    )
    return store


@pytest.fixture
async def client(op_store):
    app = FastAPI()
    app.include_router(user_projects.router)
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_operational_store] = lambda: op_store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.mark.asyncio
async def test_lists_the_accounts_projects_and_masked_keys(client, op_store):
    response = await client.get("/admin/users/user-1/projects")

    assert response.status_code == 200
    body = response.json()
    assert [p["name"] for p in body["projects"]] == ["Default", "CI"]
    assert body["projects"][1]["budget_daily_usd"] == 5
    assert body["projects"][1]["spent_today_usd"] == 1.0
    [key] = body["keys"]
    assert key["project_id"] == "proj_ci"
    assert key["key_masked"] != key["key_prefix"]
    # An administrator never gets a way to reveal the owner's saved key.
    assert key["revealable"] is False
    assert key["api_key"] is None
    assert "ciphertext" not in response.text
    op_store.list_projects.assert_awaited_once_with("user-1")
    op_store.list_account_keys.assert_awaited_once_with("user-1")


@pytest.mark.asyncio
async def test_an_unknown_user_is_404(client, op_store):
    op_store.get_user_by_id.return_value = None

    response = await client.get("/admin/users/nobody/projects")

    assert response.status_code == 404
    op_store.list_projects.assert_not_called()


@pytest.mark.asyncio
async def test_requires_admin(op_store):
    app = FastAPI()
    app.include_router(user_projects.router)
    app.dependency_overrides[get_operational_store] = lambda: op_store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/admin/users/user-1/projects")
    assert response.status_code in (401, 403)
