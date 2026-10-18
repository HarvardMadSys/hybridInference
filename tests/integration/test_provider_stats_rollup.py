"""Integration tests for provider_hourly_stats rollup."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from serving.storage.database import DatabaseLogger

if TYPE_CHECKING:
    import asyncpg

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

# The instant the frozen_clock fixture fixes, past the job's minute so the
# hour before it is due. Tests that depend on which hours are due use it, so
# they do not change behaviour when the suite runs early in an hour.
NOW = datetime(2026, 5, 10, 12, 30, tzinfo=timezone.utc)
DUE_END = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

# completed_at for markers a test writes itself; a rollup writes NOW().
SEEDED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn


@pytest.fixture
def frozen_clock(monkeypatch):
    """Fix the rollup module's clock at NOW."""
    from serving.admin import provider_stats_rollup

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(provider_stats_rollup, "datetime", FrozenDateTime)
    return NOW


async def _truncate(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for table in ("provider_hourly_stats", "provider_hourly_coverage", "api_logs"):
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
    cache_read_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cost_usd: float | None = None,
    status_code: int = 200,
    error: str | None = None,
    metadata: dict | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_logs (
                request_id, model_id, provider, timestamp,
                stream, ttft_ms, latency_ms,
                prompt_tokens, completion_tokens,
                cache_read_tokens, reasoning_tokens, cost_usd,
                status_code, error, metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb)
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
            cache_read_tokens,
            reasoning_tokens,
            cost_usd,
            status_code,
            error,
            json.dumps(metadata) if metadata else None,
        )


async def _insert_traffic(pool, hour: datetime) -> None:
    """One successful zai request ten minutes into *hour*."""
    await _insert_api_log(
        pool,
        request_id=f"traffic-{hour.isoformat()}",
        provider="zai",
        model_id="zai/glm-4.6",
        timestamp=hour + timedelta(minutes=10),
        stream=False,
        ttft_ms=None,
        latency_ms=4000,
        completion_tokens=100,
    )


async def _insert_stats_row(pool, hour: datetime, *, request_count: int = 99) -> None:
    """A zai stats row whose count _insert_traffic's rows would not produce.

    Stands for an hour rolled up earlier: a re-roll would rewrite the count.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens, total_prompt_tokens
            ) VALUES ($1, 'zai', 'zai/glm-4.6', $2, 0, 0, 0, 0)
            """,
            hour,
            request_count,
        )


async def _mark_covered(
    pool, start: datetime, end: datetime, *, skip: tuple[datetime, ...] = ()
) -> None:
    """Mark the hours in [start, end) covered, except *skip*, as of SEEDED_AT."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_coverage (hour_bucket, completed_at)
            SELECT h, $4::timestamptz
            FROM generate_series($1::timestamptz, $2::timestamptz - interval '1 hour',
                                 interval '1 hour') AS h
            WHERE h <> ALL($3::timestamptz[])
            """,
            start,
            end,
            list(skip),
            SEEDED_AT,
        )


async def _request_counts(pool) -> dict[datetime, int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT hour_bucket, request_count FROM provider_hourly_stats")
    return {row["hour_bucket"]: row["request_count"] for row in rows}


async def _coverage(pool) -> dict[datetime, datetime]:
    """Map each covered hour to its completed_at."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT hour_bucket, completed_at FROM provider_hourly_coverage")
    return {row["hour_bucket"]: row["completed_at"] for row in rows}


def _hours(start: datetime, end: datetime) -> set[datetime]:
    return {start + timedelta(hours=i) for i in range(int((end - start) / timedelta(hours=1)))}


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
async def test_run_rollup_excludes_embeddings(db_logger: DatabaseLogger):
    """Embedding rows are excluded entirely from the provider rollup.

    They have a different latency profile and no completion tokens, so they
    would skew the chat-performance KPIs that ProviderPerformanceTab reads.
    Per-user usage and the Recent Requests dashboard read api_logs directly,
    so embeddings still surface there.
    """
    from serving.admin.provider_stats_rollup import run_rollup

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc)
    # One chat completion and one embedding on the same provider/model/hour.
    await _insert_api_log(
        pool,
        request_id="r-chat",
        provider="local",
        model_id="shared-model",
        timestamp=hour + timedelta(minutes=5),
        stream=False,
        ttft_ms=None,
        latency_ms=3000,
        completion_tokens=120,
        prompt_tokens=100,
    )
    await _insert_api_log(
        pool,
        request_id="r-emb",
        provider="local",
        model_id="shared-model",
        timestamp=hour + timedelta(minutes=6),
        stream=False,
        ttft_ms=None,
        latency_ms=50,
        completion_tokens=None,
        prompt_tokens=80,
        metadata={"request_type": "embedding"},
    )

    written = await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
    assert written == 1

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM provider_hourly_stats
            WHERE provider='local' AND model_id='shared-model'
            """
        )
    assert row is not None
    # Only the chat row counts; the embedding (50ms, 80 prompt tokens) is gone.
    assert row["request_count"] == 1
    assert row["total_prompt_tokens"] == 100
    assert row["total_completion_tokens"] == 120
    assert row["latency_p50_ms"] == 3000


@pytest.mark.asyncio
async def test_run_rollup_excludes_router_placeholder_provider(db_logger: DatabaseLogger):
    """Synthetic labels are not upstream provider rollup buckets."""
    from serving.admin.provider_stats_rollup import run_rollup

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
    await _insert_api_log(
        pool,
        request_id="router-placeholder",
        provider="router",
        model_id="glm-5.2",
        timestamp=hour + timedelta(minutes=5),
        stream=False,
        ttft_ms=None,
        latency_ms=10,
        prompt_tokens=None,
        completion_tokens=None,
        status_code=404,
        error="Model 'glm-5.2' not found",
    )
    await _insert_api_log(
        pool,
        request_id="empty-placeholder",
        provider="",
        model_id="rejected-model",
        timestamp=hour + timedelta(minutes=8),
        stream=False,
        ttft_ms=None,
        latency_ms=10,
        prompt_tokens=50,
        completion_tokens=0,
        status_code=400,
        error="rejected",
    )
    await _insert_api_log(
        pool,
        request_id="upstream-row",
        provider="zai",
        model_id="glm-5.2",
        timestamp=hour + timedelta(minutes=10),
        stream=False,
        ttft_ms=None,
        latency_ms=4000,
        prompt_tokens=100,
        completion_tokens=20,
    )

    written = await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
    assert written == 1

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT provider, model_id, request_count
            FROM provider_hourly_stats
            ORDER BY provider, model_id
            """
        )

    assert [dict(row) for row in rows] == [
        {"provider": "zai", "model_id": "glm-5.2", "request_count": 1}
    ]


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


@pytest.mark.asyncio
async def test_hourly_job_rolls_up_previous_hour(db_logger: DatabaseLogger, frozen_clock):
    from serving.admin.provider_stats_rollup import hourly_job

    assert db_logger.pool is not None
    pool = db_logger.pool

    prev = DUE_END - timedelta(hours=1)

    await _insert_api_log(
        pool,
        request_id="hr-1",
        provider="chutes",
        model_id="meta/llama-3.3-70b",
        timestamp=prev + timedelta(minutes=12),
        stream=True,
        ttft_ms=500,
        latency_ms=4500,
        completion_tokens=300,
    )

    await hourly_job(pool)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT request_count, hour_bucket
            FROM provider_hourly_stats
            WHERE provider='chutes' AND model_id='meta/llama-3.3-70b'
            """
        )
    assert row is not None
    assert row["request_count"] == 1
    # The bucket equals the previous hour (hour-truncated)
    assert row["hour_bucket"] == prev
    assert prev in await _coverage(pool)


@pytest.mark.asyncio
async def test_purge_old_removes_aged_rows(db_logger: DatabaseLogger):
    from serving.admin.provider_stats_rollup import purge_old

    assert db_logger.pool is not None
    pool = db_logger.pool

    old = (datetime.now(timezone.utc) - timedelta(days=45)).replace(
        minute=0, second=0, microsecond=0
    )
    new = (datetime.now(timezone.utc) - timedelta(days=2)).replace(
        minute=0, second=0, microsecond=0
    )

    async with pool.acquire() as conn:
        for ts in (old, new):
            await conn.execute(
                """
                INSERT INTO provider_hourly_stats (
                    hour_bucket, provider, model_id,
                    request_count, error_count, stream_count,
                    total_completion_tokens
                ) VALUES ($1, 'p', 'm', 1, 0, 1, 100)
                ON CONFLICT DO NOTHING
                """,
                ts,
            )
            await _mark_covered(pool, ts, ts + timedelta(hours=1))

    deleted = await purge_old(pool, retention_days=30)
    assert deleted == 1

    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM provider_hourly_stats")
    assert n == 1
    # An hour's marker goes with its rows.
    assert set(await _coverage(pool)) == {new}


@pytest.mark.asyncio
async def test_hourly_job_skips_when_locked(db_logger: DatabaseLogger, pg_dsn: str):
    """A second concurrent hourly_job acquires no lock and returns no work."""
    import asyncpg

    from serving.admin.provider_stats_rollup import ADVISORY_LOCK_KEY, hourly_job

    assert db_logger.pool is not None
    pool = db_logger.pool

    # Hold the advisory lock on a separate session.
    holder = await asyncpg.connect(pg_dsn)
    try:
        got = await holder.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY)
        assert got is True

        # Run hourly_job: should log "lock held, skipping" and not insert.
        await hourly_job(pool)

        async with pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM provider_hourly_stats")
        assert n == 0
    finally:
        await holder.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
        await holder.close()


@pytest.mark.asyncio
async def test_backfill_missing_seeds_an_empty_table(db_logger: DatabaseLogger, frozen_clock):
    """A first start rolls up every due hour of the window, quiet ones included."""
    from serving.admin.provider_stats_rollup import backfill_missing

    assert db_logger.pool is not None
    pool = db_logger.pool

    busy = [DUE_END - timedelta(hours=n) for n in (3, 2, 1)]
    for hour in busy:
        await _insert_traffic(pool, hour)

    assert await backfill_missing(pool, days=1) == 24

    assert await _request_counts(pool) == dict.fromkeys(busy, 1)
    assert set(await _coverage(pool)) == _hours(DUE_END - timedelta(days=1), DUE_END)

    # Every hour is covered now, so a second start has nothing to do.
    assert await backfill_missing(pool, days=1) == 0


@pytest.mark.asyncio
async def test_backfill_missing_fills_a_missed_hour(db_logger: DatabaseLogger, frozen_clock):
    """An hour whose run was missed is rolled up; covered hours are left alone."""
    from serving.admin.provider_stats_rollup import backfill_missing

    assert db_logger.pool is not None
    pool = db_logger.pool

    missed = DUE_END - timedelta(hours=3)
    covered = DUE_END - timedelta(hours=5)
    await _mark_covered(pool, DUE_END - timedelta(days=1), DUE_END, skip=(missed,))
    for hour in (missed, covered):
        await _insert_traffic(pool, hour)
    await _insert_stats_row(pool, covered)

    assert await backfill_missing(pool, days=1) == 1

    assert await _request_counts(pool) == {missed: 1, covered: 99}
    coverage = await _coverage(pool)
    assert set(coverage) == _hours(DUE_END - timedelta(days=1), DUE_END)
    assert coverage[missed] != SEEDED_AT
    assert {at for hour, at in coverage.items() if hour != missed} == {SEEDED_AT}


@pytest.mark.asyncio
async def test_backfill_missing_does_no_work_on_a_complete_table(
    db_logger: DatabaseLogger, frozen_clock, monkeypatch
):
    """Startup on a complete table reads the gaps once, takes no lock, re-rolls nothing."""
    from serving.admin import provider_stats_rollup
    from serving.admin.provider_stats_rollup import backfill_missing

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = DUE_END - timedelta(hours=2)
    await _mark_covered(pool, DUE_END - timedelta(days=1), DUE_END)
    await _insert_traffic(pool, hour)
    await _insert_stats_row(pool, hour)
    lock = AsyncMock(side_effect=AssertionError("took the rollup lock"))
    monkeypatch.setattr(provider_stats_rollup, "_try_lock_run", lock)

    assert await backfill_missing(pool, days=1) == 0

    lock.assert_not_awaited()
    assert await _request_counts(pool) == {hour: 99}
    assert set((await _coverage(pool)).values()) == {SEEDED_AT}


@pytest.mark.asyncio
async def test_backfill_missing_leaves_the_hour_before_the_job_minute(
    db_logger: DatabaseLogger, monkeypatch
):
    """Before minute 5 the previous hour is not due, so a start leaves it to the job."""
    from serving.admin import provider_stats_rollup
    from serving.admin.provider_stats_rollup import backfill_missing

    assert db_logger.pool is not None
    pool = db_logger.pool

    early = DUE_END + timedelta(minutes=3)

    class EarlyDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return early if tz is None else early.astimezone(tz)

    monkeypatch.setattr(provider_stats_rollup, "datetime", EarlyDateTime)
    previous = DUE_END - timedelta(hours=1)
    await _mark_covered(pool, DUE_END - timedelta(days=2), previous)
    await _insert_traffic(pool, previous)

    assert await backfill_missing(pool, days=1) == 0
    assert await _request_counts(pool) == {}


@pytest.mark.asyncio
async def test_backfill_missing_marks_rows_from_before_coverage(
    db_logger: DatabaseLogger, frozen_clock
):
    """The first repair marks hours that already have rows instead of re-rolling them.

    That is the upgrade path: a populated table, no markers yet. Only the hours
    without rows -- quiet, or missed before this change -- are rolled up.
    """
    from serving.admin.provider_stats_rollup import backfill_missing

    assert db_logger.pool is not None
    pool = db_logger.pool

    rolled = DUE_END - timedelta(hours=4)
    missed = DUE_END - timedelta(hours=2)
    for hour in (rolled, missed):
        await _insert_traffic(pool, hour)
    await _insert_stats_row(pool, rolled)

    assert await backfill_missing(pool, days=1) == 23

    assert await _request_counts(pool) == {rolled: 99, missed: 1}
    assert set(await _coverage(pool)) == _hours(DUE_END - timedelta(days=1), DUE_END)


@pytest.mark.asyncio
async def test_hourly_job_fills_missed_hours(db_logger: DatabaseLogger, frozen_clock):
    """The job rolls up the previous hour and any earlier hour whose run was missed."""
    from serving.admin.provider_stats_rollup import hourly_job

    assert db_logger.pool is not None
    pool = db_logger.pool

    previous = DUE_END - timedelta(hours=1)
    missed = DUE_END - timedelta(hours=4)
    covered = DUE_END - timedelta(hours=6)
    await _mark_covered(pool, DUE_END - timedelta(days=1), DUE_END, skip=(previous, missed))
    for hour in (previous, missed, covered):
        await _insert_traffic(pool, hour)
    await _insert_stats_row(pool, covered)

    await hourly_job(pool, retention_days=1)

    assert await _request_counts(pool) == {previous: 1, missed: 1, covered: 99}
    assert set(await _coverage(pool)) == _hours(DUE_END - timedelta(days=1), DUE_END)


@pytest.mark.asyncio
async def test_hourly_job_rolls_up_the_previous_hour_past_one_that_keeps_failing(
    db_logger: DatabaseLogger, frozen_clock
):
    """An older hour whose rollup always fails holds up neither the previous hour nor the purge."""
    from serving.admin.provider_stats_rollup import hourly_job

    assert db_logger.pool is not None
    pool = db_logger.pool

    start = DUE_END - timedelta(days=1)
    previous = DUE_END - timedelta(hours=1)
    failing = DUE_END - timedelta(hours=3)
    expired = start - timedelta(hours=1)
    await _mark_covered(pool, start, DUE_END, skip=(previous, failing))
    await _insert_stats_row(pool, expired)
    await _insert_traffic(pool, previous)
    # Enough cost in one group that its hourly total overflows DECIMAL(14, 8).
    for i in range(101):
        await _insert_api_log(
            pool,
            request_id=f"overflow-{i}",
            provider="zai",
            model_id="zai/glm-4.6",
            timestamp=failing + timedelta(minutes=1),
            stream=False,
            ttft_ms=None,
            latency_ms=1000,
            completion_tokens=10,
            cost_usd=9999.99999999,
        )

    await hourly_job(pool, retention_days=1)

    assert await _request_counts(pool) == {previous: 1}
    coverage = await _coverage(pool)
    assert previous in coverage
    assert failing not in coverage


@pytest.mark.asyncio
async def test_hourly_job_purge_leaves_no_hour_missing(
    db_logger: DatabaseLogger, frozen_clock, monkeypatch
):
    """The purge cuts where the repair window starts, so a purged hour never reads as missing."""
    from serving.admin import provider_stats_rollup
    from serving.admin.provider_stats_rollup import backfill_missing, hourly_job

    assert db_logger.pool is not None
    pool = db_logger.pool

    start = DUE_END - timedelta(days=1)
    expired = start - timedelta(hours=1)
    await _insert_stats_row(pool, expired)
    # A complete window, plus one hour that has just passed retention.
    await _mark_covered(pool, expired, DUE_END)

    await hourly_job(pool, retention_days=1)

    assert await _request_counts(pool) == {}
    coverage = await _coverage(pool)
    assert set(coverage) == _hours(start, DUE_END)
    # Nothing in the window was rolled up again, its oldest hour included.
    assert set(coverage.values()) == {SEEDED_AT}

    lock = AsyncMock(side_effect=AssertionError("took the rollup lock"))
    monkeypatch.setattr(provider_stats_rollup, "_try_lock_run", lock)
    assert await backfill_missing(pool, days=1) == 0


@pytest.mark.asyncio
async def test_provider_hourly_stats_has_token_total_columns(db_logger: DatabaseLogger):
    """The 4 new BIGINT/DECIMAL totals exist and are nullable."""
    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'provider_hourly_stats'
              AND column_name IN (
                  'total_prompt_tokens',
                  'total_cache_read_tokens',
                  'total_reasoning_tokens',
                  'total_cost_usd'
              )
            """
        )
        by_name = {r["column_name"]: r for r in rows}

    assert set(by_name) == {
        "total_prompt_tokens",
        "total_cache_read_tokens",
        "total_reasoning_tokens",
        "total_cost_usd",
    }
    for name in ("total_prompt_tokens", "total_cache_read_tokens", "total_reasoning_tokens"):
        assert by_name[name]["data_type"] == "bigint", name
        assert by_name[name]["is_nullable"] == "YES", name
    assert by_name["total_cost_usd"]["data_type"] == "numeric"
    assert by_name["total_cost_usd"]["is_nullable"] == "YES"


@pytest.mark.asyncio
async def test_run_rollup_populates_token_totals(db_logger: DatabaseLogger):
    """Rollup sums prompt/cache_read/reasoning/cost across the hour, including errors."""
    from serving.admin.provider_stats_rollup import run_rollup

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = datetime(2026, 5, 2, 16, 0, tzinfo=timezone.utc)
    # Two successes
    await _insert_api_log(
        pool,
        request_id="tok-1",
        provider="anthropic",
        model_id="claude-opus-4-7",
        timestamp=hour + timedelta(minutes=5),
        stream=True,
        ttft_ms=300,
        latency_ms=2300,
        completion_tokens=120,
        prompt_tokens=800,
        cache_read_tokens=500,
        reasoning_tokens=40,
        cost_usd=0.01230000,
    )
    await _insert_api_log(
        pool,
        request_id="tok-2",
        provider="anthropic",
        model_id="claude-opus-4-7",
        timestamp=hour + timedelta(minutes=10),
        stream=False,
        ttft_ms=None,
        latency_ms=4000,
        completion_tokens=240,
        prompt_tokens=1200,
        cache_read_tokens=None,  # NULL → COALESCE'd to 0
        reasoning_tokens=60,
        cost_usd=0.02500000,
    )
    # One error — still counts toward token + cost totals (we paid to send)
    await _insert_api_log(
        pool,
        request_id="tok-err",
        provider="anthropic",
        model_id="claude-opus-4-7",
        timestamp=hour + timedelta(minutes=15),
        stream=True,
        ttft_ms=None,
        latency_ms=500,
        completion_tokens=None,
        prompt_tokens=300,
        cache_read_tokens=200,
        reasoning_tokens=None,
        cost_usd=0.00100000,
        status_code=500,
        error="upstream_5xx",
    )

    written = await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
    assert written == 1

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT total_prompt_tokens, total_cache_read_tokens,
                   total_reasoning_tokens, total_cost_usd
            FROM provider_hourly_stats
            WHERE provider = 'anthropic' AND model_id = 'claude-opus-4-7'
            """
        )

    assert row is not None
    assert row["total_prompt_tokens"] == 800 + 1200 + 300
    # NULL cache_read on tok-2 -> 0; tok-1 contributes 500, tok-err 200
    assert row["total_cache_read_tokens"] == 500 + 200
    # NULL reasoning on tok-err -> 0
    assert row["total_reasoning_tokens"] == 40 + 60
    # Decimal sum
    assert float(row["total_cost_usd"]) == pytest.approx(0.0123 + 0.025 + 0.001, abs=1e-9)


@pytest.mark.asyncio
async def test_run_rollup_coalesces_all_null_columns(db_logger: DatabaseLogger):
    """When every row has NULL for a SUM'd column, COALESCE returns 0 (not NULL)."""
    from serving.admin.provider_stats_rollup import run_rollup

    assert db_logger.pool is not None
    pool = db_logger.pool

    hour = datetime(2026, 5, 2, 17, 0, tzinfo=timezone.utc)
    # Seed two rows for one (provider, model) group; both have NULL for
    # cache_read_tokens, reasoning_tokens, and cost_usd. SUM over an all-NULL
    # column returns NULL in Postgres, so the only thing that turns these
    # into 0 in the stored row is the COALESCE wrapper in ROLLUP_SQL.
    for i in range(2):
        await _insert_api_log(
            pool,
            request_id=f"coal-{i}",
            provider="provider-x",
            model_id="model-y",
            timestamp=hour + timedelta(minutes=5 + i),
            stream=True,
            ttft_ms=300,
            latency_ms=2300,
            completion_tokens=120,
            prompt_tokens=500,
            cache_read_tokens=None,
            reasoning_tokens=None,
            cost_usd=None,
        )

    written = await run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
    assert written == 1

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT total_cache_read_tokens, total_reasoning_tokens, total_cost_usd
            FROM provider_hourly_stats
            WHERE provider = 'provider-x' AND model_id = 'model-y'
            """
        )

    assert row is not None
    # Without COALESCE, these would be None.
    assert row["total_cache_read_tokens"] == 0
    assert row["total_reasoning_tokens"] == 0
    assert float(row["total_cost_usd"]) == 0.0


@pytest.mark.asyncio
async def test_backfill_token_columns_fills_null_rows(db_logger: DatabaseLogger):
    """Helper detects NULL token totals and re-runs rollup hour-by-hour."""
    from serving.admin.provider_stats_rollup import backfill_token_columns

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    h = now - timedelta(hours=2)

    # Seed an api_logs row that the backfill should aggregate
    await _insert_api_log(
        pool,
        request_id="bf-tok-1",
        provider="openrouter",
        model_id="qwen/qwen3-coder",
        timestamp=h + timedelta(minutes=5),
        stream=True,
        ttft_ms=300,
        latency_ms=3300,
        completion_tokens=200,
        prompt_tokens=900,
        cache_read_tokens=400,
        reasoning_tokens=50,
        cost_usd=0.0150000,
    )

    # Insert a stats row with the legacy schema (NULL token totals).
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens
            ) VALUES ($1, 'openrouter', 'qwen/qwen3-coder', 1, 0, 1, 200)
            """,
            h,
        )

    processed = await backfill_token_columns(pool, days=1)
    assert processed > 0

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT total_prompt_tokens, total_cache_read_tokens,
                   total_reasoning_tokens, total_cost_usd
            FROM provider_hourly_stats
            WHERE hour_bucket = $1
              AND provider = 'openrouter' AND model_id = 'qwen/qwen3-coder'
            """,
            h,
        )
    assert row is not None
    assert row["total_prompt_tokens"] == 900
    assert row["total_cache_read_tokens"] == 400
    assert row["total_reasoning_tokens"] == 50
    assert float(row["total_cost_usd"]) == pytest.approx(0.015, abs=1e-9)


@pytest.mark.asyncio
async def test_backfill_token_columns_no_op_when_already_populated(
    db_logger: DatabaseLogger,
):
    """Returns 0 without iterating when no NULL rows exist."""
    from serving.admin.provider_stats_rollup import backfill_token_columns

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens, total_prompt_tokens
            ) VALUES ($1, 'p', 'm', 1, 0, 1, 100, 100)
            """,
            now - timedelta(hours=1),
        )

    processed = await backfill_token_columns(pool, days=1)
    assert processed == 0


@pytest.mark.asyncio
async def test_backfill_token_columns_is_idempotent(db_logger: DatabaseLogger):
    """Running twice does not change values."""
    from serving.admin.provider_stats_rollup import backfill_token_columns

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    h = now - timedelta(hours=2)

    await _insert_api_log(
        pool,
        request_id="bf-idem-1",
        provider="zai",
        model_id="zai/glm-4.6",
        timestamp=h + timedelta(minutes=5),
        stream=False,
        ttft_ms=None,
        latency_ms=4000,
        completion_tokens=180,
        prompt_tokens=700,
        cache_read_tokens=0,
        reasoning_tokens=0,
        cost_usd=0.005,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens
            ) VALUES ($1, 'zai', 'zai/glm-4.6', 1, 0, 0, 180)
            """,
            h,
        )

    await backfill_token_columns(pool, days=1)
    await backfill_token_columns(pool, days=1)  # second pass is a no-op (no NULL rows)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_prompt_tokens FROM provider_hourly_stats WHERE provider='zai'"
        )
    assert row["total_prompt_tokens"] == 700


async def _insert_legacy_stats_row(pool, hour_bucket: datetime, model_id: str = "m") -> None:
    """Insert a stats row shaped like one written before the token-total columns."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO provider_hourly_stats (
                hour_bucket, provider, model_id,
                request_count, error_count, stream_count,
                total_completion_tokens
            ) VALUES ($1, 'p', $2, 1, 0, 1, 100)
            """,
            hour_bucket,
            model_id,
        )


@pytest.mark.asyncio
async def test_backfill_token_columns_ignores_null_rows_outside_window(
    db_logger: DatabaseLogger,
):
    """A NULL row older than the window cannot be re-rolled, so it starts no work."""
    from serving.admin.provider_stats_rollup import backfill_token_columns

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await _insert_legacy_stats_row(pool, now - timedelta(days=3))

    assert await backfill_token_columns(pool, days=1) == 0


@pytest.mark.asyncio
async def test_backfill_token_columns_rerolls_only_chunks_with_null_rows(
    db_logger: DatabaseLogger,
):
    """A pass re-rolls only the chunks that hold NULL rows.

    These rows have no api_logs behind them, so they stay NULL, and the next
    start pays the same two chunks again rather than the whole window.
    """
    from serving.admin.provider_stats_rollup import backfill_token_columns

    assert db_logger.pool is not None
    pool = db_logger.pool

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # Two hours in the newest 6h chunk and one in the oldest of the day's four.
    await _insert_legacy_stats_row(pool, now - timedelta(hours=2), model_id="m1")
    await _insert_legacy_stats_row(pool, now - timedelta(hours=3), model_id="m2")
    await _insert_legacy_stats_row(pool, now - timedelta(hours=20), model_id="m3")

    assert await backfill_token_columns(pool, days=1) == 2
    assert await backfill_token_columns(pool, days=1) == 2

    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM provider_hourly_stats WHERE total_prompt_tokens IS NULL"
        )
    assert remaining == 3
