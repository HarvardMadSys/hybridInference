"""API tests for ``GET /admin/analytics/geo``."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices, get_db_logger, verify_admin_access
from serving.servers.routers.admin import analytics as analytics_module
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
@pytest.mark.parametrize("days", [7, 14, 30, 90])
async def test_geo_endpoint_uses_supported_complete_hour_window(app, monkeypatch, days) -> None:
    pool = object()
    app.dependency_overrides[verify_admin_access] = lambda: "admin-id"
    app.dependency_overrides[get_db_logger] = lambda: SimpleNamespace(pool=pool)
    cached = AsyncMock(return_value={"meta": {"generated_at": "2026-07-15T00:00:00+00:00"}})
    monkeypatch.setattr(analytics_module, "get_geo_demand", cached)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 15, 10, 34, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(analytics_module, "datetime", FrozenDateTime)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        params = {} if days == 14 else {"days": days}
        response = await client.get("/admin/analytics/geo", params=params)

    assert response.status_code == 200
    call = cached.await_args
    assert call.args[0] is pool
    assert call.args[1] == datetime(
        2026, 7, 15, 10, tzinfo=timezone.utc
    ) - analytics_module.timedelta(days=days)
    assert call.args[2] == datetime(2026, 7, 15, 10, tzinfo=timezone.utc)


def test_geo_endpoint_openapi_exposes_only_days_window(app) -> None:
    operation = app.openapi()["paths"]["/admin/analytics/geo"]["get"]
    query_params = [item for item in operation["parameters"] if item["in"] == "query"]
    assert [item["name"] for item in query_params] == ["days"]
    assert query_params[0]["schema"]["enum"] == [7, 14, 30, 90]


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, 1, 8, 15, 31, 89, 91])
async def test_geo_endpoint_rejects_unsupported_days(app, days) -> None:
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
