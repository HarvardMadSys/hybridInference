"""Hourly rollup of api_logs into provider_hourly_stats.

Spec: docs/superpowers/specs/2026-05-02-per-provider-hourly-performance-design.md
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

# A constant 64-bit integer so all replicas serialize on the same lock.
ADVISORY_LOCK_KEY = 0x70726F76737473  # ascii "provsts" packed

ROLLUP_SQL = """
INSERT INTO provider_hourly_stats AS p (
    hour_bucket, provider, model_id,
    request_count, error_count, stream_count,
    ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
    latency_p50_ms, latency_p95_ms, latency_p99_ms,
    throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
    prompt_tokens_avg, completion_tokens_avg, total_completion_tokens
)
SELECT
    date_trunc('hour', timestamp)                                          AS hour_bucket,
    provider,
    model_id,
    COUNT(*)                                                                AS request_count,
    COUNT(*) FILTER (WHERE status_code >= 400 OR error IS NOT NULL)         AS error_count,
    COUNT(*) FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                          AND status_code < 400 AND error IS NULL)          AS stream_count,

    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ttft_ms)
        FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                      AND status_code < 400 AND error IS NULL)::INT         AS ttft_p50_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ttft_ms)
        FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                      AND status_code < 400 AND error IS NULL)::INT         AS ttft_p95_ms,
    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ttft_ms)
        FILTER (WHERE stream = TRUE AND ttft_ms IS NOT NULL
                      AND status_code < 400 AND error IS NULL)::INT         AS ttft_p99_ms,

    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE status_code < 400 AND error IS NULL
                      AND latency_ms IS NOT NULL)::INT                      AS latency_p50_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE status_code < 400 AND error IS NULL
                      AND latency_ms IS NOT NULL)::INT                      AS latency_p95_ms,
    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms)
        FILTER (WHERE status_code < 400 AND error IS NULL
                      AND latency_ms IS NOT NULL)::INT                      AS latency_p99_ms,

    AVG(throughput_tps)                                                     AS throughput_avg_tps,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY throughput_tps)            AS throughput_p50_tps,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY throughput_tps)            AS throughput_p95_tps,

    AVG(prompt_tokens)::FLOAT                                               AS prompt_tokens_avg,
    AVG(completion_tokens)::FLOAT                                           AS completion_tokens_avg,
    COALESCE(SUM(completion_tokens), 0)::BIGINT                             AS total_completion_tokens
FROM (
    SELECT
        timestamp, provider, model_id, status_code, error,
        stream, ttft_ms, latency_ms, prompt_tokens, completion_tokens,
        CASE
            WHEN status_code >= 400
                 OR error IS NOT NULL
                 OR completion_tokens IS NULL
                 OR completion_tokens <= 0                  THEN NULL
            WHEN stream = TRUE AND ttft_ms IS NOT NULL
                 AND latency_ms > ttft_ms
                THEN completion_tokens::FLOAT / ((latency_ms - ttft_ms) / 1000.0)
            WHEN latency_ms > 0
                THEN completion_tokens::FLOAT / (latency_ms / 1000.0)
            ELSE NULL
        END AS throughput_tps
    FROM api_logs
    WHERE timestamp >= $1 AND timestamp < $2
) src
GROUP BY hour_bucket, provider, model_id
HAVING COUNT(*) > 0
ON CONFLICT (provider, model_id, hour_bucket) DO UPDATE SET
    request_count           = EXCLUDED.request_count,
    error_count             = EXCLUDED.error_count,
    stream_count            = EXCLUDED.stream_count,
    ttft_p50_ms             = EXCLUDED.ttft_p50_ms,
    ttft_p95_ms             = EXCLUDED.ttft_p95_ms,
    ttft_p99_ms             = EXCLUDED.ttft_p99_ms,
    latency_p50_ms          = EXCLUDED.latency_p50_ms,
    latency_p95_ms          = EXCLUDED.latency_p95_ms,
    latency_p99_ms          = EXCLUDED.latency_p99_ms,
    throughput_avg_tps      = EXCLUDED.throughput_avg_tps,
    throughput_p50_tps      = EXCLUDED.throughput_p50_tps,
    throughput_p95_tps      = EXCLUDED.throughput_p95_tps,
    prompt_tokens_avg       = EXCLUDED.prompt_tokens_avg,
    completion_tokens_avg   = EXCLUDED.completion_tokens_avg,
    total_completion_tokens = EXCLUDED.total_completion_tokens
"""


async def run_rollup(
    pool: asyncpg.Pool,
    *,
    start: datetime,
    end: datetime,
) -> int:
    """Aggregate api_logs in the half-open interval [start, end) into
    provider_hourly_stats. Returns number of rows affected (inserted+updated).

    Idempotent: re-running with the same window updates existing rows.
    Caller is responsible for taking the advisory lock when concurrent
    runs are possible.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be tz-aware")
    if end <= start:
        raise ValueError("end must be after start")

    async with pool.acquire() as conn:
        result = await conn.execute(ROLLUP_SQL, start, end)
    # asyncpg returns "INSERT 0 N" for INSERT statements (the 0 is oid).
    # Parse the trailing integer.
    try:
        return int(result.rsplit(" ", 1)[-1])
    except ValueError:
        return 0


PURGE_SQL = """
DELETE FROM provider_hourly_stats
WHERE hour_bucket < NOW() - $1::interval
"""


async def purge_old(pool: asyncpg.Pool, *, retention_days: int = 30) -> int:
    """Delete rows older than retention_days. Returns count deleted."""
    async with pool.acquire() as conn:
        result = await conn.execute(PURGE_SQL, timedelta(days=retention_days))
    try:
        return int(result.rsplit(" ", 1)[-1])
    except ValueError:
        return 0


async def _try_lock_run(
    pool: asyncpg.Pool,
    coro_factory,
) -> bool:
    """Acquire pg_try_advisory_lock; if it succeeds, run coro_factory(conn).

    Returns True if the work ran, False if the lock was already held.
    The lock is scoped to a dedicated connection that we hold for the
    duration of the work and release in a finally.
    """
    async with pool.acquire() as conn:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY)
        if not got:
            logger.info("rollup_provider_stats: lock held, skipping")
            return False
        try:
            await coro_factory(conn)
            return True
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)


async def hourly_job(
    pool: asyncpg.Pool,
    *,
    retention_days: int = 30,
) -> None:
    """APScheduler entrypoint. Roll up the previous full hour, then purge."""
    started_at = time.monotonic()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start, end = now - timedelta(hours=1), now

    rows_written = 0
    outcome = "error"

    async def _do(conn) -> None:
        nonlocal rows_written
        result = await conn.execute(ROLLUP_SQL, start, end)
        try:
            rows_written = int(result.rsplit(" ", 1)[-1])
        except ValueError:
            rows_written = 0
        await conn.execute(PURGE_SQL, timedelta(days=retention_days))

    try:
        ran = await _try_lock_run(pool, _do)
        outcome = "ok" if ran else "locked"
    except Exception as exc:
        logger.exception(f"rollup_provider_stats failed: {exc}")
        outcome = "error"
    finally:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "rollup_provider_stats: window=[%s, %s) rows=%d duration_ms=%d outcome=%s",
            start.isoformat(),
            end.isoformat(),
            rows_written,
            duration_ms,
            outcome,
        )
        # Metrics emission is added in Task 10.


async def backfill_if_empty(
    pool: asyncpg.Pool,
    *,
    days: int = 30,
) -> int:
    """If provider_hourly_stats has no rows, aggregate the last `days` of
    api_logs in a single pass. Idempotent: no-op when rows exist.
    Returns the number of rows written (0 when skipped).

    Multi-replica safe: takes the same advisory lock used by hourly_job,
    then re-checks emptiness inside the lock so only one replica performs
    the (potentially expensive) 30-day aggregation on first deploy.
    """
    async with pool.acquire() as conn:
        any_row = await conn.fetchval("SELECT 1 FROM provider_hourly_stats LIMIT 1")
    if any_row is not None:
        logger.info("backfill_if_empty: table populated, skipping")
        return 0

    rows_written = 0

    async def _do(conn) -> None:
        nonlocal rows_written
        # Re-check emptiness under the lock — another replica may have just
        # finished its backfill while we were waiting.
        any_row = await conn.fetchval("SELECT 1 FROM provider_hourly_stats LIMIT 1")
        if any_row is not None:
            logger.info("backfill_if_empty: populated by another replica, skipping")
            return
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        result = await conn.execute(ROLLUP_SQL, start, end)
        try:
            rows_written = int(result.rsplit(" ", 1)[-1])
        except ValueError:
            rows_written = 0
        logger.info(
            "backfill_if_empty: window=[%s, %s) rows=%d",
            start.isoformat(),
            end.isoformat(),
            rows_written,
        )

    ran = await _try_lock_run(pool, _do)
    if not ran:
        logger.info("backfill_if_empty: lock held by another replica, skipping")
    return rows_written


def register_rollup_job(scheduler, pool: asyncpg.Pool) -> None:
    """Register the hourly rollup job on the existing AsyncIOScheduler.

    Fires at minute 5 every hour to give the previous hour's writes
    time to flush.
    """
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        hourly_job,
        trigger=CronTrigger(minute=5, timezone=timezone.utc),
        args=[pool],
        id="rollup_provider_stats",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    logger.info("rollup_provider_stats: registered on scheduler (cron minute=5)")
