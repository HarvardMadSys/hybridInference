"""Tests for the admin /admin/api/provider-observability route."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices, get_db_logger, verify_admin_access
from serving.servers.routers import admin as admin_router


def _build_admin_app(db_logger=None) -> FastAPI:
    """Build a minimal FastAPI app with the admin router mounted."""
    app = FastAPI(title="Admin Provider Observability Test")
    services = AppServices(
        router=MagicMock(),
        db_logger=db_logger,
        routing_manager=None,
    )
    app.state.services = services  # type: ignore[attr-defined]
    app.include_router(admin_router.router)
    return app


def _override_admin(app: FastAPI) -> None:
    async def _fake_admin() -> str:
        return "admin@test"

    app.dependency_overrides[verify_admin_access] = _fake_admin


class _FakeAcquire:
    def __init__(self, conn: _FakeConnection):
        self.conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeConnection:
    def __init__(self, fetchrow_response, fetch_responses):
        self.fetchrow_response = fetchrow_response
        self.fetch_responses = list(fetch_responses)
        self.fetchrow_calls = []
        self.fetch_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_response

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.fetch_responses.pop(0)


class _FakePool:
    def __init__(self, fetchrow_response=None, fetch_responses=None):
        self.conn = _FakeConnection(fetchrow_response, fetch_responses or [])

    def acquire(self):
        return _FakeAcquire(self.conn)


def _fake_db_logger(pool) -> MagicMock:
    db_logger = MagicMock()
    db_logger.pool = pool
    return db_logger


@pytest.mark.asyncio
async def test_provider_observability_requires_admin_auth():
    app = _build_admin_app(db_logger=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-observability",
            params={"provider": "openai"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_provider_observability_no_db_returns_503():
    fake_db_logger = _fake_db_logger(pool=None)
    app = _build_admin_app(db_logger=fake_db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-observability",
            params={"provider": "openai"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_provider_observability_rejects_synthetic_provider():
    fake_db_logger = _fake_db_logger(pool=MagicMock())
    app = _build_admin_app(db_logger=fake_db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-observability",
            params={"provider": "router"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 400
    assert "upstream provider" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_provider_observability_range_over_90_days_rejected():
    fake_db_logger = _fake_db_logger(pool=MagicMock())
    app = _build_admin_app(db_logger=fake_db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-observability",
            params={
                "provider": "openai",
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-05-01T00:00:00Z",
            },
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 400
    assert "90 days" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_provider_observability_rejects_inverted_range():
    fake_db_logger = _fake_db_logger(pool=MagicMock())
    app = _build_admin_app(db_logger=fake_db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-observability",
            params={
                "provider": "openai",
                "from": "2026-01-02T00:00:00Z",
                "to": "2026-01-01T00:00:00Z",
            },
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 400
    assert "must be after" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_provider_observability_happy_path_from_api_logs():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    next_bucket = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    fake_pool = _FakePool(
        fetchrow_response={
            "request_count": 10,
            "error_count": 3,
            "rate_limited_count": 2,
            "timeout_count": 1,
            "server_error_count": 1,
            "cache_eligible_count": 8,
            "cache_hit_count": 3,
            "input_tokens": 3000,
            "cache_read_tokens": 900,
            "cache_write_tokens": 120,
        },
        fetch_responses=[
            [
                {
                    "start_time": start,
                    "request_count": 4,
                    "error_count": 1,
                    "cache_eligible_count": 3,
                    "cache_hit_count": 1,
                    "cache_read_tokens": 250,
                    "input_tokens": 900,
                },
                {
                    "start_time": next_bucket,
                    "request_count": 6,
                    "error_count": 2,
                    "cache_eligible_count": 5,
                    "cache_hit_count": 2,
                    "cache_read_tokens": 650,
                    "input_tokens": 2100,
                },
            ],
            [
                {"error_type": "rate_limited", "count": 2},
                {"error_type": "timeout", "count": 1},
            ],
            [
                {
                    "model_id": "gpt-4o-mini",
                    "request_count": 10,
                    "error_count": 3,
                    "cache_eligible_count": 8,
                    "cache_hit_count": 3,
                    "cache_read_tokens": 900,
                    "input_tokens": 3000,
                },
            ],
        ],
    )
    fake_db_logger = _fake_db_logger(pool=fake_pool)
    app = _build_admin_app(db_logger=fake_db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-observability",
            params={
                "provider": "openai",
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-01-01T02:00:00Z",
            },
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["bucket_minutes"] == 5
    assert body["totals"]["request_count"] == 10
    assert body["totals"]["rate_limited_count"] == 2
    assert body["totals"]["cache_hit_count"] == 3
    assert body["buckets"][0]["start_time"].startswith("2026-01-01T00:00:00")
    assert body["error_types"][0] == {
        "error_type": "rate_limited",
        "count": 2,
        "fraction": pytest.approx(2 / 3),
    }
    assert body["models"][0]["model_id"] == "gpt-4o-mini"
    assert "status_codes" not in body
    assert "top_errors" not in body
    assert "request_type" in fake_pool.conn.fetchrow_calls[0][0]
    assert len(fake_pool.conn.fetch_calls) == 3
