"""Tests for the GET /admin/analytics endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        # The active-users count and turn averages are a single fetchrow.
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow = AsyncMock(
            return_value={"active_users": 7, "avg_turns": 12.5, "avg_user_turns": 6.0}
        )
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
        assert body["avg_turns"] == 12.5
        assert body["avg_user_turns"] == 6.0
        # Empty fetch() returns; lists must still be present for the schema.
        assert body["sparkline"] == []
        assert body["top_users"] == []
        assert body["by_model"] == []
        assert body["by_provider"] == []
        assert body["by_model_top_users"] == []
        assert "generated_at" in body

    @pytest.mark.asyncio
    async def test_route_builds_by_model_top_users(self, admin_app, mock_db_logger):
        """Flat per-(model, user) rows are grouped into models with nested users."""
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        by_model_top_users_rows = [
            {
                "model_id": "claude-sonnet-4-6",
                "user_id": "u1",
                "email": "alice@example.com",
                "req_count": 120,
                "token_count": 90000,
                "model_req_count": 200,
                "model_token_count": 150000,
            },
            {
                "model_id": "claude-sonnet-4-6",
                "user_id": "u2",
                "email": "bob@example.com",
                "req_count": 80,
                "token_count": 60000,
                "model_req_count": 200,
                "model_token_count": 150000,
            },
            {
                "model_id": "gpt-4o",
                "user_id": "u1",
                "email": "alice@example.com",
                "req_count": 50,
                "token_count": 30000,
                "model_req_count": 50,
                "model_token_count": 30000,
            },
        ]
        # conn.fetch call order in the endpoint: top_users, by_model, by_provider,
        # by_model_top_users, sparkline.
        mock_conn.fetch = AsyncMock(side_effect=[[], [], [], by_model_top_users_rows, []])

        async def _fake_admin() -> str:
            return "admin@test"

        admin_app.dependency_overrides[verify_admin_access] = _fake_admin

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/analytics?period=day")
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        by_model_top_users = resp.json()["by_model_top_users"]
        # Models preserve the DESC-by-request ranking from the query.
        assert [m["model"] for m in by_model_top_users] == ["claude-sonnet-4-6", "gpt-4o"]

        sonnet = by_model_top_users[0]
        assert sonnet["requests"] == 200
        assert sonnet["tokens"] == 150000
        assert [u["email"] for u in sonnet["users"]] == ["alice@example.com", "bob@example.com"]
        assert sonnet["users"][0]["requests"] == 120
        assert sonnet["users"][0]["tokens"] == 90000

        gpt = by_model_top_users[1]
        assert gpt["model"] == "gpt-4o"
        assert len(gpt["users"]) == 1

    @pytest.mark.asyncio
    async def test_route_handles_period_with_no_chat_requests(self, admin_app, mock_db_logger):
        """AVG over only non-chat requests returns NULL → averages serialize as null."""
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow = AsyncMock(
            return_value={"active_users": 0, "avg_turns": None, "avg_user_turns": None}
        )

        async def _fake_admin() -> str:
            return "admin@test"

        admin_app.dependency_overrides[verify_admin_access] = _fake_admin

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/analytics?period=day")
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["avg_turns"] is None
        assert body["avg_user_turns"] is None

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


class TestAdminGrowthRoute:
    """GET /admin/analytics/growth — the daily DAU / token series and its slope."""

    @staticmethod
    def _today_utc() -> datetime:
        """The UTC midnight the endpoint treats as the still-filling day."""
        return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    @pytest.fixture
    def growth_app(self, mock_db_logger):
        app = FastAPI(title="Admin Growth Test")
        services = AppServices(
            router=MagicMock(),
            db_logger=mock_db_logger,
            routing_manager=None,
        )
        app.state.services = services  # type: ignore[attr-defined]
        app.include_router(admin_router.router)
        return app

    @staticmethod
    def _as_admin(app):
        async def _fake_admin() -> str:
            return "admin@test"

        app.dependency_overrides[verify_admin_access] = _fake_admin

    @pytest.mark.asyncio
    async def test_route_requires_admin_auth(self, growth_app):
        transport = ASGITransport(app=growth_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/analytics/growth")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_route_rejects_unsupported_range(self, growth_app):
        self._as_admin(growth_app)
        transport = ASGITransport(app=growth_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/analytics/growth?days=45")
        growth_app.dependency_overrides.clear()
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_todays_partial_day_is_returned_but_not_fitted(self, growth_app, mock_db_logger):
        """The still-filling day is charted, yet kept out of every trend number.

        Four complete days climb by a clean +10 DAU/day. Today has only one user
        so far; folding it into the fit would collapse the slope to ~0.2 and
        report a healthy week as flat.
        """
        today = self._today_utc()
        rows = [
            {
                "day": today - timedelta(days=4 - i),
                "active_users": users,
                "new_users": users,
                "tokens": users * 1000,
                "requests": users * 3,
            }
            for i, users in enumerate([10, 20, 30, 40])
        ]
        rows.append(
            {
                "day": today,
                "active_users": 1,
                "new_users": 0,
                "tokens": 1000,
                "requests": 3,
            }
        )
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetch = AsyncMock(return_value=rows)

        self._as_admin(growth_app)
        transport = ASGITransport(app=growth_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/analytics/growth?days=30")
        growth_app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["days"] == 30
        # Every bucket the query returned is charted, today included.
        assert len(body["points"]) == 5
        assert [p["partial"] for p in body["points"]] == [False, False, False, False, True]

        assert body["users_trend"]["slope_per_day"] == pytest.approx(10.0)
        assert body["tokens_trend"]["slope_per_day"] == pytest.approx(10000.0)
        # Halves of the four complete days: (10, 20) then (30, 40).
        assert body["users_trend"]["compare_days"] == 2
        assert body["users_trend"]["previous_avg"] == pytest.approx(15.0)
        assert body["users_trend"]["recent_avg"] == pytest.approx(35.0)
        assert body["users_trend"]["change_pct"] == pytest.approx(20.0 / 15.0)

    @pytest.mark.asyncio
    async def test_range_with_no_traffic_returns_zeroed_trends(self, growth_app, mock_db_logger):
        """A gap-filled range of empty days must not divide by zero."""
        today = self._today_utc()
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "day": today - timedelta(days=2 - i),
                    "active_users": 0,
                    "new_users": 0,
                    "tokens": 0,
                    "requests": 0,
                }
                for i in range(3)
            ]
        )

        self._as_admin(growth_app)
        transport = ASGITransport(app=growth_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/analytics/growth?days=30")
        growth_app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["users_trend"]["slope_per_day"] == 0.0
        # Flat zero has no baseline to grow from, so no percentage is claimed.
        assert body["users_trend"]["change_pct"] is None
        assert body["tokens_trend"]["change_pct"] is None
