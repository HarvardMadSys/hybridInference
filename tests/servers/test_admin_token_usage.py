"""Tests for the admin /admin/api/provider-token-usage route."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import (
    AppServices,
    get_db_logger,
    verify_admin_access,
)
from serving.servers.routers import admin as admin_router


def _build_admin_app(db_logger=None) -> FastAPI:
    app = FastAPI(title="Admin Token Usage Test")
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
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""

    async def fetch(self, query, *args):
        del args
        self.query = query
        return self.rows


class _FakePool:
    def __init__(self, rows):
        self.conn = _FakeConn(rows)

    def acquire(self):
        return _FakeAcquire(self.conn)


# ---------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------


class TestTokenUsageAuth:
    @pytest.mark.asyncio
    async def test_route_requires_admin_auth(self):
        app = _build_admin_app(db_logger=None)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/api/provider-token-usage")
        assert resp.status_code == 401


class TestTokenUsageValidation:
    @pytest.mark.asyncio
    async def test_invalid_range_rejected(self):
        fake_db_logger = MagicMock()
        fake_db_logger.pool = MagicMock()

        app = _build_admin_app(db_logger=fake_db_logger)
        _override_admin(app)
        app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/admin/api/provider-token-usage",
                params={"range": "5m"},
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_no_db_returns_503(self):
        fake_db_logger = MagicMock()
        fake_db_logger.pool = None

        app = _build_admin_app(db_logger=fake_db_logger)
        _override_admin(app)
        app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/api/provider-token-usage")
        app.dependency_overrides.clear()
        assert resp.status_code == 503


@pytest.mark.asyncio
async def test_token_usage_hides_router_placeholder_rows():
    """Regression: synthetic labels are placeholders, not upstream providers."""
    rows = [
        {
            "provider": "router",
            "model_id": "glm-5.2",
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0,
            "request_count": 113,
        },
        {
            "provider": "",
            "model_id": "rejected-model",
            "input_tokens": 50,
            "output_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0,
            "request_count": 7,
        },
        {
            "provider": "zai",
            "model_id": "glm-5.2",
            "input_tokens": 100,
            "output_tokens": 20,
            "cached_tokens": 80,
            "reasoning_tokens": 5,
            "cost_usd": 0.0123,
            "request_count": 2,
        },
    ]
    fake_db_logger = MagicMock()
    fake_db_logger.pool = _FakePool(rows)

    app = _build_admin_app(db_logger=fake_db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/api/provider-token-usage")
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["provider"] for r in body["rows"]] == ["zai"]
    assert body["totals"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_tokens": 80,
        "reasoning_tokens": 5,
        "cost_usd": 0.0123,
        "request_count": 2,
    }
    assert "provider NOT IN ('', 'router')" in fake_db_logger.pool.conn.query


# ---------------------------------------------------------------------
# Real Postgres happy-path tests (TEST_PG_DSN required)
# ---------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn


@pytest_asyncio.fixture
async def db_logger(pg_dsn: str):
    from serving.storage.database import DatabaseLogger

    logger = DatabaseLogger({"dsn": pg_dsn}, store_full_prompts=False)
    await logger.initialize()
    assert logger.pool is not None
    async with logger.pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE provider_hourly_stats")
    try:
        yield logger
    finally:
        assert logger.pool is not None
        async with logger.pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE provider_hourly_stats")
        await logger.cleanup()


async def _seed(
    pool,
    *,
    provider: str,
    model_id: str,
    bucket: datetime,
    input_tokens: int,
    output_tokens: int,
    cached: int,
    reasoning: int,
    cost: float,
    requests: int = 1,
):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens,
                total_prompt_tokens, total_cache_read_tokens,
                total_reasoning_tokens, total_cost_usd
            )
            VALUES ($1, $2, $3, $4, 0, 0, $5, $6, $7, $8, $9)
            ON CONFLICT (provider, model_id, hour_bucket) DO UPDATE SET
                total_prompt_tokens     = EXCLUDED.total_prompt_tokens,
                total_completion_tokens = EXCLUDED.total_completion_tokens,
                total_cache_read_tokens = EXCLUDED.total_cache_read_tokens,
                total_reasoning_tokens  = EXCLUDED.total_reasoning_tokens,
                total_cost_usd          = EXCLUDED.total_cost_usd,
                request_count           = EXCLUDED.request_count
            """,
            bucket,
            provider,
            model_id,
            requests,
            output_tokens,
            input_tokens,
            cached,
            reasoning,
            cost,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_usage_24h_happy_path(db_logger):
    """One row per (provider, model_id), sorted DESC by total tokens, totals correct."""
    pool = db_logger.pool
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    bucket = now - timedelta(hours=2)

    # Heavy provider/model
    await _seed(
        pool,
        provider="anthropic",
        model_id="claude-opus-4-7",
        bucket=bucket,
        input_tokens=1_000_000,
        output_tokens=200_000,
        cached=80_000,
        reasoning=10_000,
        cost=12.345,
        requests=400,
    )
    # Lighter provider/model
    await _seed(
        pool,
        provider="openrouter",
        model_id="qwen/qwen3-coder",
        bucket=bucket,
        input_tokens=100_000,
        output_tokens=20_000,
        cached=5_000,
        reasoning=0,
        cost=0.50,
        requests=50,
    )

    app = _build_admin_app(db_logger=db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-token-usage",
            params={"range": "24h"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["range"] == "24h"
    assert len(body["rows"]) == 2
    # Sorted DESC: anthropic first (heavier total)
    assert body["rows"][0]["provider"] == "anthropic"
    assert body["rows"][0]["input_tokens"] == 1_000_000
    assert body["rows"][0]["output_tokens"] == 200_000
    assert body["rows"][0]["cached_tokens"] == 80_000
    assert body["rows"][0]["reasoning_tokens"] == 10_000
    assert body["rows"][0]["request_count"] == 400
    assert body["rows"][0]["cost_usd"] == pytest.approx(12.345, abs=1e-6)

    assert body["rows"][1]["provider"] == "openrouter"

    # Totals = sum of rows
    assert body["totals"]["input_tokens"] == 1_100_000
    assert body["totals"]["output_tokens"] == 220_000
    assert body["totals"]["cached_tokens"] == 85_000
    assert body["totals"]["reasoning_tokens"] == 10_000
    assert body["totals"]["request_count"] == 450
    assert body["totals"]["cost_usd"] == pytest.approx(12.845, abs=1e-6)

    # refreshed_at is hour-truncated (no subhour, no minutes/seconds)
    refreshed = datetime.fromisoformat(body["refreshed_at"].replace("Z", "+00:00"))
    assert refreshed.minute == 0 and refreshed.second == 0 and refreshed.microsecond == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_usage_empty_window_returns_zero_totals(db_logger):
    """No rows in window -> 200 with rows=[] and zero totals."""
    pool = db_logger.pool

    # Insert a row 60 days ago — falls outside any selectable range.
    old = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=60)
    await _seed(
        pool,
        provider="zai",
        model_id="zai/glm-4.6",
        bucket=old,
        input_tokens=10,
        output_tokens=10,
        cached=0,
        reasoning=0,
        cost=0.001,
    )

    app = _build_admin_app(db_logger=db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-token-usage",
            params={"range": "30d"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["totals"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0,
        "request_count": 0,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_usage_window_inclusion(db_logger):
    """Rows inside [from, to) included; rows outside excluded."""
    pool = db_logger.pool
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # 2h ago: inside the 24h window
    await _seed(
        pool,
        provider="p1",
        model_id="m1",
        bucket=now - timedelta(hours=2),
        input_tokens=100,
        output_tokens=10,
        cached=0,
        reasoning=0,
        cost=0.001,
    )
    # 25h ago: outside the 24h window
    await _seed(
        pool,
        provider="p2",
        model_id="m2",
        bucket=now - timedelta(hours=25),
        input_tokens=999,
        output_tokens=999,
        cached=999,
        reasoning=999,
        cost=9.99,
    )

    app = _build_admin_app(db_logger=db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-token-usage",
            params={"range": "24h"},
        )
    app.dependency_overrides.clear()

    body = resp.json()
    providers = [r["provider"] for r in body["rows"]]
    assert providers == ["p1"]
    assert body["totals"]["input_tokens"] == 100
