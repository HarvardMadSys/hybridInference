"""Tests for the admin /admin/api/provider-stats route."""

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
    """Build a minimal FastAPI app with the admin router mounted."""
    app = FastAPI(title="Admin Provider Stats Test")
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


# ---------------------------------------------------------------------
# Auth + range-cap tests (mocked db_logger)
# ---------------------------------------------------------------------


class TestProviderStatsAuth:
    @pytest.mark.asyncio
    async def test_route_requires_admin_auth(self):
        app = _build_admin_app(db_logger=None)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/admin/api/provider-stats",
                params={"provider": "openrouter", "model_id": "qwen/qwen3-coder"},
            )
        assert resp.status_code == 401


class TestProviderStatsRangeCap:
    @pytest.mark.asyncio
    async def test_range_over_90_days_rejected(self):
        # Use a fake db_logger with a non-empty pool so we get past the 503 guard.
        fake_db_logger = MagicMock()
        fake_db_logger.pool = MagicMock()  # truthy

        app = _build_admin_app(db_logger=fake_db_logger)
        _override_admin(app)
        # Override get_db_logger as well, since the dep would otherwise
        # come from app.state.services and may differ.
        app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/admin/api/provider-stats",
                params={
                    "provider": "openrouter",
                    "model_id": "qwen/qwen3-coder",
                    "from": "2024-01-01T00:00:00Z",
                    "to": "2026-01-01T00:00:00Z",
                },
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 400
        assert "90 days" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_to_before_from_rejected(self):
        fake_db_logger = MagicMock()
        fake_db_logger.pool = MagicMock()

        app = _build_admin_app(db_logger=fake_db_logger)
        _override_admin(app)
        app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/admin/api/provider-stats",
                params={
                    "provider": "openrouter",
                    "model_id": "qwen/qwen3-coder",
                    "from": "2026-01-02T00:00:00Z",
                    "to": "2026-01-01T00:00:00Z",
                },
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_naive_datetime_rejected(self):
        fake_db_logger = MagicMock()
        fake_db_logger.pool = MagicMock()

        app = _build_admin_app(db_logger=fake_db_logger)
        _override_admin(app)
        app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/admin/api/provider-stats",
                params={
                    "provider": "openrouter",
                    "model_id": "qwen/qwen3-coder",
                    "from": "2026-01-01T00:00:00",
                },
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 400
        assert "timezone-aware" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_no_db_returns_503(self):
        fake_db_logger = MagicMock()
        fake_db_logger.pool = None

        app = _build_admin_app(db_logger=fake_db_logger)
        _override_admin(app)
        app.dependency_overrides[get_db_logger] = lambda: fake_db_logger

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/admin/api/provider-stats",
                params={"provider": "openrouter", "model_id": "qwen/qwen3-coder"},
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 503


# ---------------------------------------------------------------------
# Happy-path test against a real Postgres (TEST_PG_DSN required)
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_stats_happy_path(db_logger):
    """Insert a row in provider_hourly_stats and verify the route returns it."""
    pool = db_logger.pool
    assert pool is not None

    # Pick an hour bucket inside the default 7-day window
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    bucket = now - timedelta(hours=2)
    other_bucket = now - timedelta(hours=3)
    # A bucket OUTSIDE the default 7-day window — its provider/model must still
    # surface in the dropdown lists (regression: lists are not window-bound).
    stale_bucket = now - timedelta(days=20)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
                latency_p50_ms, latency_p95_ms, latency_p99_ms,
                throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
                prompt_tokens_avg, completion_tokens_avg, total_completion_tokens
            )
            VALUES ($1, 'openrouter', 'qwen/qwen3-coder',
                    5, 1, 3,
                    800, 1100, 1200,
                    7600, 12000, 12200,
                    36.1, 40.0, 44.4,
                    100.0, 245.0, 980)
            """,
            bucket,
        )
        # A row for a different provider/model in the same window — should
        # appear in `providers`/`models` but not in `rows`.
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens
            )
            VALUES ($1, 'chutes', 'meta/llama-3.3-70b', 2, 0, 2, 400)
            """,
            other_bucket,
        )
        # A row well OUTSIDE the 7-day window — must NOT appear in `rows` but
        # MUST appear in the `providers`/`models` dropdown lists.
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens
            )
            VALUES ($1, 'deepseek', 'deepseek/deepseek-chat', 1, 0, 1, 200)
            """,
            stale_bucket,
        )

    app = _build_admin_app(db_logger=db_logger)
    _override_admin(app)
    app.dependency_overrides[get_db_logger] = lambda: db_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-stats",
            params={
                "provider": "openrouter",
                "model_id": "qwen/qwen3-coder",
            },
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["rows"], list)
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["provider"] == "openrouter"
    assert row["model_id"] == "qwen/qwen3-coder"
    assert row["request_count"] == 5
    assert row["error_count"] == 1
    assert row["stream_count"] == 3
    assert row["ttft_p50_ms"] == 800
    assert row["latency_p50_ms"] == 7600
    assert row["total_completion_tokens"] == 980

    # The windowed `rows` must exclude the stale out-of-window deepseek row.
    assert all(r["provider"] != "deepseek" for r in body["rows"])

    # Distinct provider/model lists span the full table, including the stale
    # out-of-window row — proving the dropdowns are not bound to the range.
    assert set(body["providers"]) == {"openrouter", "chutes", "deepseek"}
    assert set(body["models"]) == {
        "qwen/qwen3-coder",
        "meta/llama-3.3-70b",
        "deepseek/deepseek-chat",
    }

    # window_providers is range-scoped: the in-window providers only, used by
    # the UI to pick a default that has data. The stale deepseek row is excluded.
    assert set(body["window_providers"]) == {"openrouter", "chutes"}
