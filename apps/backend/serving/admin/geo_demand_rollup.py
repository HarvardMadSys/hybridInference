"""Persistent privacy-safe hourly country rollups for geo analytics."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from serving.analytics.geo_demand import (
    MAX_WINDOW,
    build_hours_index,
    consume_geo_rows,
    normalize_window,
)
from serving.utils.geo_resolver import GeoResolver
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    import asyncpg

logger = get_logger(__name__)

ADVISORY_LOCK_KEY = 0x67656F646D6E64  # ascii "geodmnd" packed
BACKFILL_CHUNK_HOURS = 6
RETENTION_DAYS = 90

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS geo_hourly_coverage (
        hour_bucket  TIMESTAMPTZ PRIMARY KEY,
        rows_total   BIGINT      NOT NULL CHECK (rows_total >= 0),
        rows_with_ip BIGINT      NOT NULL CHECK (
            rows_with_ip >= 0 AND rows_with_ip <= rows_total
        ),
        completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS geo_hourly_demand (
        hour_bucket      TIMESTAMPTZ NOT NULL REFERENCES geo_hourly_coverage(hour_bucket)
            ON DELETE CASCADE,
        country_code     TEXT        NOT NULL,
        continent_code   TEXT        NOT NULL,
        request_count    BIGINT      NOT NULL CHECK (request_count >= 0),
        completion_tokens BIGINT     NOT NULL,
        PRIMARY KEY (hour_bucket, country_code, continent_code)
    )
    """,
)

DELETE_WINDOW_SQL = """
DELETE FROM geo_hourly_demand
WHERE hour_bucket >= $1 AND hour_bucket < $2
"""

UPSERT_COVERAGE_SQL = """
INSERT INTO geo_hourly_coverage AS c (
    hour_bucket, rows_total, rows_with_ip, completed_at
) VALUES ($1, $2, $3, NOW())
ON CONFLICT (hour_bucket) DO UPDATE SET
    rows_total = EXCLUDED.rows_total,
    rows_with_ip = EXCLUDED.rows_with_ip,
    completed_at = EXCLUDED.completed_at
"""

INSERT_DEMAND_SQL = """
INSERT INTO geo_hourly_demand (
    hour_bucket, country_code, continent_code, request_count, completion_tokens
) VALUES ($1, $2, $3, $4, $5)
"""

COVERED_HOURS_SQL = """
SELECT hour_bucket
FROM geo_hourly_coverage
WHERE hour_bucket >= $1 AND hour_bucket < $2
"""

PURGE_SQL = """
DELETE FROM geo_hourly_coverage
WHERE hour_bucket < $1
"""


async def ensure_geo_rollup_schema(connection: Any) -> None:
    """Create the country rollup and explicit coverage tables idempotently."""
    for statement in _SCHEMA_STATEMENTS:
        await connection.execute(statement)


async def rollup_window(
    connection: Any,
    *,
    start: datetime,
    end: datetime,
    resolver: GeoResolver,
    prefetch: int = 5_000,
) -> int:
    """Atomically replace one completed window from SQL-grouped raw log rows."""
    start, end = normalize_window(start, end)
    if not resolver.country_enabled:
        raise RuntimeError("country GeoIP database is unavailable")

    async with connection.transaction():
        buckets, coverage = await consume_geo_rows(
            connection,
            start,
            end,
            resolver,
            prefetch=prefetch,
        )
        await connection.execute(DELETE_WINDOW_SQL, start, end)
        await connection.executemany(
            UPSERT_COVERAGE_SQL,
            [(hour, item.rows_total, item.rows_with_ip) for hour, item in coverage.items()],
        )
        if buckets:
            await connection.executemany(
                INSERT_DEMAND_SQL,
                [
                    (hour, country, continent, bucket.n, bucket.tout)
                    for (hour, country, continent), bucket in buckets.items()
                ],
            )
    return len(buckets)


async def run_rollup(
    pool: asyncpg.Pool,
    *,
    start: datetime,
    end: datetime,
    resolver_factory: Callable[[], GeoResolver] = GeoResolver,
) -> int:
    """Aggregate one window without taking the scheduler advisory lock."""
    resolver = resolver_factory()
    try:
        async with pool.acquire() as connection:
            return await rollup_window(
                connection,
                start=start,
                end=end,
                resolver=resolver,
            )
    finally:
        resolver.close()


def _chunk_missing_hours(
    hours: list[datetime],
    *,
    chunk_hours: int,
) -> list[tuple[datetime, datetime]]:
    """Combine sorted missing hours into bounded contiguous windows."""
    if chunk_hours < 1:
        raise ValueError("chunk_hours must be positive")
    windows: list[tuple[datetime, datetime]] = []
    unique_hours = sorted(set(hours))
    if not unique_hours:
        return windows

    start = unique_hours[0]
    end = start + timedelta(hours=1)
    for hour in unique_hours[1:]:
        if hour == end and end - start < timedelta(hours=chunk_hours):
            end += timedelta(hours=1)
            continue
        windows.append((start, end))
        start = hour
        end = hour + timedelta(hours=1)
    windows.append((start, end))
    return windows


async def _repair_missing(
    connection: Any,
    *,
    start: datetime,
    end: datetime,
    resolver_factory: Callable[[], GeoResolver],
    force_hours: tuple[datetime, ...] = (),
    chunk_hours: int = BACKFILL_CHUNK_HOURS,
    newest_first: bool = False,
) -> tuple[int, int]:
    """Fill coverage gaps and optionally refresh selected completed hours."""
    start, end = normalize_window(start, end)
    covered = {row["hour_bucket"] for row in await connection.fetch(COVERED_HOURS_SQL, start, end)}
    missing = [hour for hour in build_hours_index(start, end) if hour not in covered]
    missing.extend(hour for hour in force_hours if start <= hour < end)
    windows = _chunk_missing_hours(missing, chunk_hours=chunk_hours)
    if newest_first:
        windows.reverse()

    resolver = resolver_factory()
    rows_written = 0
    try:
        for window_start, window_end in windows:
            rows_written += await rollup_window(
                connection,
                start=window_start,
                end=window_end,
                resolver=resolver,
            )
    finally:
        resolver.close()
    return len(set(missing)), rows_written


async def _try_lock_run(
    pool: asyncpg.Pool,
    work: Callable[[Any], Coroutine[Any, Any, None]],
) -> bool:
    """Run work while holding this rollup's session-scoped advisory lock."""
    async with pool.acquire() as connection:
        got = await connection.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY)
        if not got:
            logger.info("rollup_geo_demand: lock held, skipping")
            return False
        try:
            await work(connection)
            return True
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)


async def hourly_job(
    pool: asyncpg.Pool,
    *,
    retention_days: int = RETENTION_DAYS,
    resolver_factory: Callable[[], GeoResolver] = GeoResolver,
) -> None:
    """Refresh the previous hour and repair any covered-window gaps."""
    started_at = time.monotonic()
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - min(timedelta(days=retention_days), MAX_WINDOW)
    previous_hour = end - timedelta(hours=1)
    hours_processed = 0
    rows_written = 0
    outcome = "error"

    async def _do(connection: Any) -> None:
        nonlocal hours_processed, rows_written
        hours_processed, rows_written = await _repair_missing(
            connection,
            start=start,
            end=end,
            resolver_factory=resolver_factory,
            force_hours=(previous_hour,),
            newest_first=True,
        )
        await connection.execute(PURGE_SQL, start)

    try:
        ran = await _try_lock_run(pool, _do)
        outcome = "ok" if ran else "locked"
    except Exception as exc:
        logger.exception("rollup_geo_demand failed: %s", exc)
    finally:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "rollup_geo_demand: window=[%s, %s) hours=%d rows=%d duration_ms=%d outcome=%s",
            start.isoformat(),
            end.isoformat(),
            hours_processed,
            rows_written,
            duration_ms,
            outcome,
        )


async def backfill_missing(
    pool: asyncpg.Pool,
    *,
    days: int = RETENTION_DAYS,
    resolver_factory: Callable[[], GeoResolver] = GeoResolver,
) -> int:
    """Fill missing hourly coverage in bounded chunks without blocking startup."""
    if days < 1 or timedelta(days=days) > MAX_WINDOW:
        raise ValueError("days must be between 1 and 90")
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    hours_processed = 0
    rows_written = 0

    async def _do(connection: Any) -> None:
        nonlocal hours_processed, rows_written
        hours_processed, rows_written = await _repair_missing(
            connection,
            start=start,
            end=end,
            resolver_factory=resolver_factory,
            newest_first=True,
        )

    ran = await _try_lock_run(pool, _do)
    if ran:
        logger.info(
            "backfill_geo_demand: window=[%s, %s) hours=%d rows=%d",
            start.isoformat(),
            end.isoformat(),
            hours_processed,
            rows_written,
        )
    return hours_processed


def register_rollup_job(scheduler: Any, pool: asyncpg.Pool) -> None:
    """Register the multi-replica-safe hourly geo rollup job."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        hourly_job,
        trigger=CronTrigger(minute=10, timezone=timezone.utc),
        args=[pool],
        id="rollup_geo_demand",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    logger.info("rollup_geo_demand: registered on scheduler (cron minute=10)")
