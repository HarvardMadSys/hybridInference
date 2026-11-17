"""Tests for the API-key concurrency limits endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from serving.servers.concurrency import (
    COUNT_TOKENS_USER_CONCURRENCY_LIMIT,
    EXEMPT_MODEL_USER_CONCURRENCY_LIMIT,
)
from serving.servers.routers import limits


@pytest.fixture
def runtime_settings(monkeypatch):
    rt = AsyncMock()
    rt.get_int.return_value = 7
    monkeypatch.setattr(limits, "get_runtime_settings_instance", lambda: rt)
    return rt


@pytest.mark.asyncio
async def test_role_cap_is_used_without_an_override(runtime_settings):
    body = await limits.get_limits({"user_id": "u1", "role": "pro"})

    runtime_settings.get_int.assert_awaited_once_with("user_concurrency_pro")
    assert body["user"] == {"role": "pro", "source": "role", "max_concurrency": 7}
    assert body["project"] is None
    assert body["org"] is None


@pytest.mark.asyncio
async def test_override_wins_and_bounds_the_special_budgets(runtime_settings):
    body = await limits.get_limits({"user_id": "u1", "role": "free", "max_concurrent_requests": 2})

    runtime_settings.get_int.assert_not_awaited()
    assert body["user"] == {"role": "free", "source": "override", "max_concurrency": 2}
    assert body["exempt_models"]["max_concurrency"] == 2
    assert body["count_tokens"]["max_concurrency"] == 2


@pytest.mark.asyncio
async def test_zero_means_unlimited_and_does_not_lower_special_budgets(runtime_settings):
    body = await limits.get_limits({"user_id": "u1", "role": "admin", "max_concurrent_requests": 0})

    assert body["user"]["max_concurrency"] == 0
    assert body["exempt_models"]["max_concurrency"] == 0
    assert body["count_tokens"]["max_concurrency"] == 0


@pytest.mark.asyncio
async def test_default_special_budgets_apply_without_an_override(runtime_settings):
    body = await limits.get_limits({"user_id": "u1", "role": "free"})

    assert body["exempt_models"]["max_concurrency"] == EXEMPT_MODEL_USER_CONCURRENCY_LIMIT
    assert body["count_tokens"]["max_concurrency"] == COUNT_TOKENS_USER_CONCURRENCY_LIMIT


@pytest.mark.asyncio
async def test_project_and_org_caps_are_reported_when_present(runtime_settings):
    body = await limits.get_limits(
        {
            "user_id": "u1",
            "role": "free",
            "project_id": "prj_1",
            "project_name": "Team",
            "project_max_concurrent_requests": 12,
            "org_id": "org_1",
            "org_max_concurrent_requests": None,
        }
    )

    assert body["project"] == {"id": "prj_1", "name": "Team", "max_concurrency": 12}
    assert body["org"] == {"id": "org_1", "max_concurrency": None}


def test_route_is_registered():
    assert [route.path for route in limits.router.routes] == ["/v1/limits"]


@pytest.mark.asyncio
async def test_route_serves_the_limits_for_the_authenticated_key(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setattr(limits, "get_runtime_settings_instance", lambda: None)

    app = FastAPI()
    app.include_router(limits.router)
    app.dependency_overrides[limits.verify_api_key_for_balance] = lambda: {
        "user_id": "u1",
        "role": "free",
        "max_concurrent_requests": 5,
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/limits")

    assert response.status_code == 200
    assert response.json()["user"]["max_concurrency"] == 5
