"""Integration tests for provider_hourly_stats rollup."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from serving.storage.database import DatabaseLogger

if TYPE_CHECKING:
    import asyncpg

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn


async def _truncate(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for table in ("provider_hourly_stats", "api_logs"):
            exists = await conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
            if exists:
                await conn.execute(f"TRUNCATE TABLE {table}")


@pytest_asyncio.fixture
async def db_logger(pg_dsn: str):
    logger = DatabaseLogger({"dsn": pg_dsn}, store_full_prompts=False)
    await logger.initialize()
    assert logger.pool is not None
    await _truncate(logger.pool)
    try:
        yield logger
    finally:
        assert logger.pool is not None
        await _truncate(logger.pool)
        await logger.cleanup()


@pytest.mark.asyncio
async def test_provider_hourly_stats_schema(db_logger: DatabaseLogger):
    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'provider_hourly_stats'
            """
        )
        cols = {r["column_name"] for r in rows}

    expected = {
        "hour_bucket",
        "provider",
        "model_id",
        "request_count",
        "error_count",
        "stream_count",
        "ttft_p50_ms",
        "ttft_p95_ms",
        "ttft_p99_ms",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "throughput_avg_tps",
        "throughput_p50_tps",
        "throughput_p95_tps",
        "prompt_tokens_avg",
        "completion_tokens_avg",
        "total_completion_tokens",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


@pytest.mark.asyncio
async def test_provider_hourly_stats_primary_key(db_logger: DatabaseLogger):
    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        pk_cols = await conn.fetch(
            """
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid
                                AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'public.provider_hourly_stats'::regclass
              AND i.indisprimary
            ORDER BY array_position(i.indkey, a.attnum)
            """
        )
        names = [r["column_name"] for r in pk_cols]
    assert names == ["provider", "model_id", "hour_bucket"]
