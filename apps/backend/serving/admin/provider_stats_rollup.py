"""Hourly rollup of api_logs into provider_hourly_stats.

Spec: docs/agents/specs/archive/2026-05-02-per-provider-hourly-performance-design.md

Each hour the hourly job or the startup backfill rolls up also gets a row in
``provider_hourly_coverage``, written in the same transaction as the hour's
stats. Quiet hours get one too:
``provider_hourly_stats`` has no row for an hour without traffic, so the marker
is the only way to tell such an hour from one whose run never happened -- a
restart spanning the job's minute, a crash, or a run that found another replica
holding the lock. The hourly job and the startup backfill roll up every due
hour of the retention window that has no marker, so a missed hour is filled by
the next run or start instead of reading as zero traffic for good.
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

# Days of hourly rows and coverage markers kept. The purge's cutoff is also
# the oldest hour a repair fills, so a purged hour never reads as missing.
RETENTION_DAYS = 30

# The job fires at this minute past every hour, to give the previous hour's
# writes time to flush. An hour is due for its rollup from then on.
ROLLUP_MINUTE = 5

# Hours per UPSERT chunk in backfill_token_columns. 6h gives ~120 chunks
# over a 30-day window — fewer round-trips than per-hour, still bounded
# enough that any single statement stays well under a minute on the
# already-indexed (provider, timestamp) range scan.
BACKFILL_CHUNK_HOURS = 6

_HOUR = timedelta(hours=1)

ROLLUP_SQL = """
INSERT INTO provider_hourly_stats AS p (
    hour_bucket, provider, model_id,
    request_count, error_count, stream_count,
    ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
    latency_p50_ms, latency_p95_ms, latency_p99_ms,
    throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
    prompt_tokens_avg, completion_tokens_avg, total_completion_tokens,
    total_prompt_tokens, total_cache_read_tokens,
    total_reasoning_tokens, total_cost_usd
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
    COALESCE(SUM(completion_tokens), 0)::BIGINT                             AS total_completion_tokens,

    COALESCE(SUM(prompt_tokens), 0)::BIGINT                                 AS total_prompt_tokens,
    COALESCE(SUM(cache_read_tokens), 0)::BIGINT                             AS total_cache_read_tokens,
    COALESCE(SUM(reasoning_tokens), 0)::BIGINT                              AS total_reasoning_tokens,
    COALESCE(SUM(cost_usd), 0)::DECIMAL(14, 8)                              AS total_cost_usd
FROM (
    SELECT
        timestamp, provider, model_id, status_code, error,
        stream, ttft_ms, latency_ms, prompt_tokens, completion_tokens,
        cache_read_tokens, reasoning_tokens, cost_usd,
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
      AND provider NOT IN ('', 'router')
      -- Exclude embeddings entirely from the provider rollup: they have a
      -- different latency profile and no completion tokens, so they'd skew the
      -- chat-performance KPIs (request/error counts, ttft/latency/throughput,
      -- prefill tokens) that ProviderPerformanceTab reads. Per-user usage and
      -- the Recent Requests dashboard read api_logs directly, so embeddings
      -- still surface there; provider billing spend uses
      -- query_provider_hourly_spend (also api_logs), so cost is unaffected.
      AND (metadata->>'request_type') IS DISTINCT FROM 'embedding'
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
    total_completion_tokens = EXCLUDED.total_completion_tokens,
    total_prompt_tokens     = EXCLUDED.total_prompt_tokens,
    total_cache_read_tokens = EXCLUDED.total_cache_read_tokens,
    total_reasoning_tokens  = EXCLUDED.total_reasoning_tokens,
    total_cost_usd          = EXCLUDED.total_cost_usd
"""

# The hours in [$1, $2) that no rollup has marked as covered, newest first. On
# a complete table this returns no rows.
MISSING_HOURS_SQL = """
SELECT h.hour_bucket
FROM generate_series($1::timestamptz, $2::timestamptz - interval '1 hour', interval '1 hour')
    AS h(hour_bucket)
WHERE NOT EXISTS (
    SELECT 1 FROM provider_hourly_coverage c WHERE c.hour_bucket = h.hour_bucket
)
ORDER BY h.hour_bucket DESC
"""

MARK_COVERED_SQL = """
INSERT INTO provider_hourly_coverage (hour_bucket, completed_at) VALUES ($1, NOW())
ON CONFLICT (hour_bucket) DO UPDATE SET completed_at = EXCLUDED.completed_at
"""

# Coverage starts on a table that may already hold rolled-up hours. An hour
# with rows was rolled up, so it is marked rather than rolled up again: a
# second pass would add nothing, and after api_logs rows are deleted it would
# rewrite counts of traffic that did happen.
SEED_COVERAGE_SQL = """
INSERT INTO provider_hourly_coverage (hour_bucket)
SELECT DISTINCT hour_bucket FROM provider_hourly_stats
ON CONFLICT (hour_bucket) DO NOTHING
"""

PURGE_SQL = """
DELETE FROM provider_hourly_stats
WHERE hour_bucket < $1
"""

PURGE_COVERAGE_SQL = """
DELETE FROM provider_hourly_coverage
WHERE hour_bucket < $1
"""


def _row_count(result: str) -> int:
    """Parse the row count from an asyncpg command tag such as ``INSERT 0 N``."""
    try:
        return int(result.rsplit(" ", 1)[-1])
    except ValueError:
        return 0


def _due_end(now: datetime) -> datetime:
    """End of the newest hour due for its rollup at *now*.

    That is the start of the current hour from minute ``ROLLUP_MINUTE`` on, and
    the start of the previous hour before it.
    """
    due = now - timedelta(minutes=ROLLUP_MINUTE)
    return due.replace(minute=0, second=0, microsecond=0)


async def run_rollup(
    pool: asyncpg.Pool,
    *,
    start: datetime,
    end: datetime,
) -> int:
    """Aggregate api_logs into provider_hourly_stats over [start, end).

    Returns the number of rows affected (inserted+updated). Idempotent:
    re-running with the same window updates existing rows. Caller is
    responsible for taking the advisory lock when concurrent runs are
    possible. Marks no hour covered; the hourly job and the backfill do.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be tz-aware")
    if end <= start:
        raise ValueError("end must be after start")

    async with pool.acquire() as conn:
        result = await conn.execute(ROLLUP_SQL, start, end)
    return _row_count(result)


async def _roll_up_hour(conn: asyncpg.Connection, hour: datetime) -> int:
    """Roll up the hour starting at *hour* and mark it covered.

    One transaction, so the hour is marked only once its rows are written.
    Returns the number of stats rows written.
    """
    async with conn.transaction():
        result = await conn.execute(ROLLUP_SQL, hour, hour + _HOUR)
        await conn.execute(MARK_COVERED_SQL, hour)
    return _row_count(result)


async def _repair_missing(
    conn: asyncpg.Connection,
    *,
    start: datetime,
    end: datetime,
) -> tuple[int, int]:
    """Roll up the hours in ``[start, end)`` that have no coverage marker.

    One hour per statement, so a repair is never heavier than the job's usual
    run, and newest first: an older hour that keeps failing cannot hold up the
    previous hour's rollup. Run it under the advisory lock. Returns ``(hours
    rolled up, stats rows written)``.
    """
    if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM provider_hourly_coverage)"):
        await conn.execute(SEED_COVERAGE_SQL)
    missing = [row["hour_bucket"] for row in await conn.fetch(MISSING_HOURS_SQL, start, end)]
    rows_written = 0
    for hour in missing:
        rows_written += await _roll_up_hour(conn, hour)
    return len(missing), rows_written


async def _purge(conn: asyncpg.Connection, *, cutoff: datetime) -> int:
    """Delete the stats rows and coverage markers of the hours before *cutoff*.

    Returns the number of stats rows deleted.
    """
    async with conn.transaction():
        result = await conn.execute(PURGE_SQL, cutoff)
        await conn.execute(PURGE_COVERAGE_SQL, cutoff)
    return _row_count(result)


async def purge_old(pool: asyncpg.Pool, *, retention_days: int = RETENTION_DAYS) -> int:
    """Delete rows and coverage markers older than retention_days.

    The cutoff is the hour the hourly job's window starts at, so no hour is
    purged that a repair would then find missing. Returns stats rows deleted.
    """
    cutoff = _due_end(datetime.now(timezone.utc)) - timedelta(days=retention_days)
    async with pool.acquire() as conn:
        return await _purge(conn, cutoff=cutoff)


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
    retention_days: int = RETENTION_DAYS,
) -> None:
    """Purge old rows, then roll up every due hour that is still missing.

    At the scheduled minute that is the previous hour, plus any hour of the
    retention window whose run was missed, including an hour a run of this job
    skipped because another replica held the lock.

    Used as the APScheduler entrypoint registered by ``register_rollup_job``.
    """
    started_at = time.monotonic()
    end = _due_end(datetime.now(timezone.utc))
    start = end - timedelta(days=retention_days)

    hours_rolled = 0
    rows_written = 0
    outcome = "error"

    async def _do(conn) -> None:
        nonlocal hours_rolled, rows_written
        # Purge first: a repair that fails part-way must not keep old rows.
        await _purge(conn, cutoff=start)
        hours_rolled, rows_written = await _repair_missing(conn, start=start, end=end)

    try:
        ran = await _try_lock_run(pool, _do)
        outcome = "ok" if ran else "locked"
    except Exception as exc:
        logger.exception(f"rollup_provider_stats failed: {exc}")
        outcome = "error"
    finally:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "rollup_provider_stats: window=[%s, %s) hours=%d rows=%d duration_ms=%d outcome=%s",
            start.isoformat(),
            end.isoformat(),
            hours_rolled,
            rows_written,
            duration_ms,
            outcome,
        )
        # Metrics emission is added in Task 10.


async def backfill_missing(
    pool: asyncpg.Pool,
    *,
    days: int = RETENTION_DAYS,
) -> int:
    """Roll up every due hour of the last ``days`` days that has no coverage marker.

    Every hour of the window on a new table, otherwise the hours that came due
    while no hourly job ran. On a complete table this is one indexed query and
    takes no lock. Otherwise it takes the advisory lock used by ``hourly_job`` and
    re-reads what is missing under it, so replicas starting together do the
    work once; a replica that finds the lock held skips, and the next hourly
    run fills whatever is still missing. ``days`` should not exceed the job's
    retention, or every start rolls up hours the job has just purged.

    Returns the number of hours rolled up (0 when skipped).
    """
    end = _due_end(datetime.now(timezone.utc))
    start = end - timedelta(days=days)
    async with pool.acquire() as conn:
        missing = await conn.fetch(MISSING_HOURS_SQL, start, end)
    if not missing:
        logger.info("backfill_provider_stats: no missing hours, skipping")
        return 0

    hours_rolled = 0

    async def _do(conn) -> None:
        nonlocal hours_rolled
        hours_rolled, rows_written = await _repair_missing(conn, start=start, end=end)
        logger.info(
            "backfill_provider_stats: window=[%s, %s) hours=%d rows=%d",
            start.isoformat(),
            end.isoformat(),
            hours_rolled,
            rows_written,
        )

    ran = await _try_lock_run(pool, _do)
    if not ran:
        logger.info("backfill_provider_stats: lock held by another replica, skipping")
    return hours_rolled


async def backfill_token_columns(
    pool: asyncpg.Pool,
    *,
    days: int = 30,
) -> int:
    """Re-run the rollup over the chunks that hold NULL token-total columns.

    Fills rows written before the migration that added total_prompt_tokens /
    total_cache_read_tokens / total_reasoning_tokens / total_cost_usd to
    provider_hourly_stats. Startup calls it on every boot, so it looks only
    at the last ``days`` days -- the window it re-rolls -- and skips chunks
    without a NULL row. A row it cannot fill, older than the window or with
    no api_logs rows left behind it, therefore never costs a whole-window
    re-roll on each start. No-op when the window has no NULL rows.

    Returns the number of chunks processed (0 when skipped). Idempotent:
    the same UPSERT runs as the hourly job, so calling repeatedly is safe.
    """
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    async with pool.acquire() as conn:
        any_null = await conn.fetchval(
            "SELECT 1 FROM provider_hourly_stats WHERE total_prompt_tokens IS NULL "
            "AND hour_bucket >= $1 AND hour_bucket < $2 LIMIT 1",
            start,
            end,
        )
    if any_null is None:
        logger.info("backfill_token_columns: no NULL rows in window, skipping")
        return 0

    processed = 0

    async def _do(conn) -> None:
        nonlocal processed
        null_hours = await conn.fetch(
            "SELECT DISTINCT hour_bucket FROM provider_hourly_stats "
            "WHERE total_prompt_tokens IS NULL AND hour_bucket >= $1 AND hour_bucket < $2",
            start,
            end,
        )
        if not null_hours:
            logger.info("backfill_token_columns: filled by another replica, skipping")
            return

        # Chunk the window so we issue ~120 UPSERTs over 30 days instead
        # of 720, while still bounding each statement to a small enough
        # window that it cannot lock the table for long. Only the chunks
        # holding a NULL row are re-rolled.
        chunk = timedelta(hours=BACKFILL_CHUNK_HOURS)
        chunk_starts = sorted(
            {start + (row["hour_bucket"] - start) // chunk * chunk for row in null_hours}
        )
        for chunk_start in chunk_starts:
            await conn.execute(ROLLUP_SQL, chunk_start, min(chunk_start + chunk, end))
            processed += 1
        logger.info(
            "backfill_token_columns: window=[%s, %s) chunks=%d chunk_hours=%d",
            start.isoformat(),
            end.isoformat(),
            processed,
            BACKFILL_CHUNK_HOURS,
        )

    ran = await _try_lock_run(pool, _do)
    if not ran:
        logger.info("backfill_token_columns: lock held by another replica, skipping")
    return processed


def register_rollup_job(scheduler, pool: asyncpg.Pool) -> None:
    """Register the hourly rollup job on the existing AsyncIOScheduler.

    Fires at minute ``ROLLUP_MINUTE`` every hour to give the previous hour's
    writes time to flush.
    """
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        hourly_job,
        trigger=CronTrigger(minute=ROLLUP_MINUTE, timezone=timezone.utc),
        args=[pool],
        id="rollup_provider_stats",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    logger.info("rollup_provider_stats: registered on scheduler (cron minute=%d)", ROLLUP_MINUTE)
