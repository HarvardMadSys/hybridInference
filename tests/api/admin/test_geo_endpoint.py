"""API tests for ``GET /admin/analytics/geo``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.analytics.geo_demand import geo_demand_cache
from serving.servers.deps import AppServices, get_db_logger, verify_admin_access
from serving.servers.routers.admin.analytics import router


@pytest.fixture
def app():
    application = FastAPI()
    application.state.services = AppServices(router=MagicMock())
    application.include_router(router)
    return application


@pytest.mark.asyncio
async def test_geo_endpoint_requires_admin(app) -> None:
    app.dependency_overrides[get_db_logger] = lambda: SimpleNamespace(pool=object())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/analytics/geo")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_geo_endpoint_uses_db_pool_and_since_overrides_days(app, monkeypatch) -> None:
    pool = object()
    app.dependency_overrides[verify_admin_access] = lambda: "admin-id"
    app.dependency_overrides[get_db_logger] = lambda: SimpleNamespace(pool=pool)
    cached = AsyncMock(return_value={"meta": {"generated_at": "2026-07-15T00:00:00+00:00"}})
    monkeypatch.setattr(geo_demand_cache, "get", cached)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/analytics/geo",
            params={
                "days": 1,
                "since": "2026-07-01T02:34:00+00:00",
                "until": "2026-07-03T05:45:00+00:00",
            },
        )

    assert response.status_code == 200
    call = cached.await_args
    assert call.args[0] is pool
    assert call.args[1].isoformat() == "2026-07-01T02:00:00+00:00"
    assert call.args[2].isoformat() == "2026-07-03T05:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"since": "2026-07-01T00:00:00", "until": "2026-07-02T00:00:00Z"}, "timezone"),
        (
            {"since": "2026-07-02T00:00:00Z", "until": "2026-07-01T00:00:00Z"},
            "before until",
        ),
        (
            {"since": "2026-01-01T00:00:00Z", "until": "2026-07-01T00:00:00Z"},
            "90 days",
        ),
    ],
)
async def test_geo_endpoint_rejects_invalid_windows(app, params, detail) -> None:
    app.dependency_overrides[verify_admin_access] = lambda: "admin-id"
    app.dependency_overrides[get_db_logger] = lambda: SimpleNamespace(pool=object())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/analytics/geo", params=params)
    assert response.status_code == 422
    assert detail in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, 91])
async def test_geo_endpoint_rejects_days_outside_bounds(app, days) -> None:
    app.dependency_overrides[verify_admin_access] = lambda: "admin-id"
    app.dependency_overrides[get_db_logger] = lambda: SimpleNamespace(pool=object())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/analytics/geo", params={"days": days})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_geo_endpoint_requires_configured_database(app) -> None:
    app.dependency_overrides[verify_admin_access] = lambda: "admin-id"
    app.dependency_overrides[get_db_logger] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/analytics/geo")
    assert response.status_code == 500
    assert response.json()["detail"] == "Database not configured"
