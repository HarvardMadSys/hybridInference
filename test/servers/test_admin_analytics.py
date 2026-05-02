"""Tests for the GET /admin/analytics endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin as admin_router


class TestAdminAnalyticsRoute:
    @pytest.fixture
    def admin_app(self, mock_db_logger):
        """Build a minimal FastAPI app with the admin router mounted.

        Wires the shared ``mock_db_logger`` fixture from conftest so the
        endpoint sees a usable mock connection pool. Tests that need
        specific row data attach AsyncMocks to the underlying mock conn.
        """
        # Configure the mock connection's fetchrow / fetch as awaitables so
        # the production code (which uses one connection sequentially) works.
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow = AsyncMock(return_value={"cnt": 7})
        mock_conn.fetch = AsyncMock(return_value=[])

        app = FastAPI(title="Admin Analytics Test")
        services = AppServices(
            router=MagicMock(),
            db_logger=mock_db_logger,
            routing_manager=None,
        )
        app.state.services = services  # type: ignore[attr-defined]
        app.include_router(admin_router.router)
        return app

    @pytest.mark.asyncio
    async def test_route_requires_admin_auth(self, admin_app):
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/analytics")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_route_returns_analytics_for_valid_period(self, admin_app):
        async def _fake_admin() -> str:
            return "admin@test"

        admin_app.dependency_overrides[verify_admin_access] = _fake_admin

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/analytics?period=day")
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["period"] == "day"
        assert body["active_users"] == 7
        # Empty fetch() returns; lists must still be present for the schema.
        assert body["sparkline"] == []
        assert body["top_users"] == []
        assert body["by_model"] == []
        assert body["by_provider"] == []
        assert "generated_at" in body

    @pytest.mark.asyncio
    async def test_route_rejects_invalid_period(self, admin_app):
        async def _fake_admin() -> str:
            return "admin@test"

        admin_app.dependency_overrides[verify_admin_access] = _fake_admin

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/analytics?period=bogus")
        admin_app.dependency_overrides.clear()

        # FastAPI's Literal validation rejects unknown values with 422.
        assert resp.status_code == 422
