"""Integration tests for provider_hourly_stats rollup."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
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


async def _insert_api_log(
    pool,
    *,
    request_id: str,
    provider: str,
    model_id: str,
    timestamp: datetime,
    stream: bool,
    ttft_ms: int | None,
    latency_ms: int,
    completion_tokens: int | None,
    prompt_tokens: int | None = 100,
    status_code: int = 200,
    error: str | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_logs (
                request_id, model_id, provider, timestamp,
                stream, ttft_ms, latency_ms,
                prompt_tokens, completion_tokens,
                status_code, error
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            request_id,
            model_id,
            provider,
            timestamp,
            stream,
            ttft_ms,
            latency_ms,
            prompt_tokens,
            completion_tokens,
            status_code,
            error,
        )


@pytest.mark.asyncio
async def test_run_rollup_aggregates_one_hour(db_logger: DatabaseLogger):
    from serving.admin.provider_stats_rollup import run_rollup

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = datetime(2026, 5, 2, 13, 0, tzinfo=timezone.utc)
    # 3 successful streaming requests on (openrouter, qwen)
    for i, (ttft, latency, ctok) in enumerate(
        [(400, 5400, 200), (800, 9800, 400), (1200, 12200, 220)]
    ):
        await _insert_api_log(
            pool,
            request_id=f"r-stream-{i}",
            provider="openrouter",
            model_id="qwen/qwen3-coder",
            timestamp=hour + timedelta(minutes=10 + i),
            stream=True,
            ttft_ms=ttft,
            latency_ms=latency,
            completion_tokens=ctok,
        )
    # 1 error
    await _insert_api_log(
        pool,
        request_id="r-err",
        provider="openrouter",
        model_id="qwen/qwen3-coder",
        timestamp=hour + timedelta(minutes=20),
        stream=True,
        ttft_ms=None,
        latency_ms=2000,
        completion_tokens=None,
        status_code=500,
        error="upstream_timeout",
    )
    # 1 non-stream success
    await _insert_api_log(
        pool,
        request_id="r-nostream",
        provider="openrouter",
        model_id="qwen/qwen3-coder",
        timestamp=hour + timedelta(minutes=30),
        stream=False,
        ttft_ms=None,
        latency_ms=4000,
        completion_tokens=160,
    )

    written = await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
    assert written == 1

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM provider_hourly_stats
            WHERE provider='openrouter' AND model_id='qwen/qwen3-coder'
            """
        )
    assert row is not None
    assert row["request_count"] == 5
    assert row["error_count"] == 1
    assert row["stream_count"] == 3  # 3 successful streaming with ttft
    # TTFT p50 of [400, 800, 1200] = 800
    assert row["ttft_p50_ms"] == 800
    # All non-error latencies: [5400, 9800, 12200, 4000] -> p50 ~ 7600
    assert 7000 <= row["latency_p50_ms"] <= 8200
    # Throughput per stream row: ctok / ((latency-ttft)/1000)
    #   r-stream-0: 200 / 5.0 = 40.0
    #   r-stream-1: 400 / 9.0 ≈ 44.44
    #   r-stream-2: 220 / 11.0 = 20.0
    #   r-nostream: 160 / 4.0 = 40.0
    # avg ≈ 36.11
    assert 33.0 <= row["throughput_avg_tps"] <= 40.0
    assert row["total_completion_tokens"] == 200 + 400 + 220 + 160


@pytest.mark.asyncio
async def test_run_rollup_is_idempotent(db_logger: DatabaseLogger):
    from serving.admin.provider_stats_rollup import run_rollup

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
    for i in range(3):
        await _insert_api_log(
            pool,
            request_id=f"idem-{i}",
            provider="anthropic",
            model_id="claude-opus-4-7",
            timestamp=hour + timedelta(minutes=5 + i),
            stream=True,
            ttft_ms=300 + i * 50,
            latency_ms=2000 + i * 100,
            completion_tokens=120,
        )

    await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
    await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))

    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM provider_hourly_stats")
        row = await conn.fetchrow("SELECT request_count FROM provider_hourly_stats LIMIT 1")
    assert n == 1
    assert row["request_count"] == 3
