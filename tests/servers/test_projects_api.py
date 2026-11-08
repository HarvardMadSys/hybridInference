"""Project console API: ownership, budget validation and archiving."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import (
    get_current_user,
    get_db_logger,
    get_embedding_adapters,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.routers import projects

_ROW = {
    "id": "proj_eval",
    "name": "Eval",
    "description": None,
    "is_default": False,
    "budget_daily_usd": None,
    "budget_monthly_usd": None,
    "allowed_models": None,
    "max_concurrent_requests": None,
    "created_at": datetime(2026, 11, 1, tzinfo=UTC),
}


@pytest.fixture
def op_store():
    store = MagicMock()
    store.list_projects = AsyncMock(return_value=[dict(_ROW)])
    store.get_project = AsyncMock(return_value=dict(_ROW))
    store.get_default_project = AsyncMock(
        return_value=dict(_ROW, id="proj_default", name="Default")
    )
    store.create_project = AsyncMock(return_value=dict(_ROW))
    store.update_project = AsyncMock(return_value=dict(_ROW))
    store.archive_project = AsyncMock(return_value=2)
    store.get_project_spend = AsyncMock(return_value=(1.5, 10.0))
    store.count_project_keys = AsyncMock(return_value=3)
    store.get_account_budgets = AsyncMock(return_value=(100.0, 500.0))
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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.mark.asyncio
async def test_list_projects_includes_spend_and_key_count(client):
    response = await client.get("/user/projects")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "proj_eval",
            "name": "Eval",
            "description": None,
            "is_default": False,
            "budget_daily_usd": None,
            "budget_monthly_usd": None,
            "allowed_models": None,
            "max_concurrent_requests": None,
            "spent_today_usd": 1.5,
            "spent_month_usd": 10.0,
            "key_count": 3,
            "created_at": "2026-11-01T00:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_create_project_rejects_a_budget_above_the_account(client, op_store):
    response = await client.post(
        "/user/projects", json={"name": "Too big", "budget_daily_usd": 101}
    )

    assert response.status_code == 422
    op_store.create_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_project_rejects_a_duplicate_name(client, op_store):
    op_store.list_projects.return_value = [dict(_ROW, name="Eval")]

    with patch.object(projects, "log_admin_action", new_callable=AsyncMock):
        response = await client.post("/user/projects", json={"name": "eval"})

    assert response.status_code == 409
    op_store.create_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_project_returns_it(client, op_store):
    op_store.list_projects.return_value = []
    with (
        patch.object(projects, "log_admin_action", new_callable=AsyncMock),
        patch.object(
            projects, "_canonical_allowlist", new_callable=AsyncMock, return_value=["m-1"]
        ),
    ):
        response = await client.post(
            "/user/projects",
            json={
                "name": "Eval",
                "description": "Evaluation jobs",
                "budget_daily_usd": 10,
                "allowed_models": ["m-1"],
            },
        )

    assert response.status_code == 201
    assert response.json()["id"] == "proj_eval"
    kwargs = op_store.create_project.call_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs["name"] == "Eval"
    assert kwargs["description"] == "Evaluation jobs"
    assert kwargs["allowed_models"] == ["m-1"]


@pytest.mark.asyncio
async def test_another_accounts_project_is_a_404(client, op_store):
    op_store.get_project.return_value = None

    response = await client.get("/user/projects/proj_someone_else")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_default_project_cannot_be_archived(client, op_store):
    op_store.get_project.return_value = dict(_ROW, is_default=True, id="proj_default")

    response = await client.post("/user/projects/proj_default/archive")

    assert response.status_code == 409
    op_store.archive_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_returns_the_revoked_key_count(client, op_store):
    response = await client.post("/user/projects/proj_eval/archive")

    assert response.status_code == 200
    assert response.json()["keys_revoked"] == 2
    op_store.archive_project.assert_awaited_once_with("user-1", "proj_eval")


@pytest.mark.asyncio
async def test_project_name_cannot_be_cleared(client, op_store):
    response = await client.patch("/user/projects/proj_eval", json={"name": None})

    assert response.status_code == 422
    op_store.update_project.assert_not_awaited()


@pytest.mark.parametrize("payload", [{"status": None}, {"budget_daily_usd": "1e20"}])
def test_org_settings_reject_values_the_database_cannot_store(payload):
    from pydantic import ValidationError

    from serving.servers.routers.admin.orgs import OrgUpdateRequest

    with pytest.raises(ValidationError):
        OrgUpdateRequest.model_validate(payload)
