"""Daily rollup behind the admin Analytics tab's growth series.

``/admin/analytics/growth`` charts, for each UTC day of the last 30, 60 or 90,
the active users, new users, requests and tokens, and fits a trend to them. It
used to group every ``api_logs`` row of the range by (day, user) on each load --
three months of the largest table, for 90 points.

So each completed day is counted once into ``daily_activity``: its request
count, its prompt + completion tokens -- both over every row, signed in or not,
as the series counts them -- and the sorted ids of the signed-in users active
that day. Active users are the length of that list. New users are not stored:
the series counts a user as new on their first active day *within the range
asked for*, which a per-day number cannot know, so they are derived from the
lists at read time.

A read takes the stored days and counts the rest live from ``api_logs`` -- the
days from :data:`user_usage_rollup.SETTLED_DAY_SQL` on, and any earlier day of
the range not stored (not filled yet, invalidated, or counted under another
:data:`DEFINITION`) -- in one statement, so one snapshot.

Only the last :data:`RETENTION_DAYS` days are kept: the series looks back 90.

The user lists name accounts, so whatever deletes ``api_logs`` rows drops the
stored days those rows were on in the same transaction (:func:`invalidate_days`):
"Clear errors", and a user's hard delete, after which the days are counted
again without the user. Every fill and invalidation holds one transaction-scoped
advisory lock, as in :mod:`serving.storage.user_usage_rollup`.
"""

from __future__ import annotations

import hashlib
import time
from datetime import timedelta, timezone
from typing import TYPE_CHECKING, Any

from serving.storage.user_usage_rollup import NO_JIT_SQL, SETTLED_DAY_SQL
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    import asyncpg

logger = get_logger(__name__)

# Held by every write to the table. A constant 64-bit key, so writers on every
# replica serialize on the same lock.
ADVISORY_LOCK_KEY = 0x6461696C7961  # ascii "dailya" packed

# Days kept: the growth series' longest range, 90 days, plus slack so a late
# hourly run never purges a day it still shows.
RETENTION_DAYS = 95

# What a day stores, as aggregates over that day's api_logs rows (alias l).
# The fill and the live part of a read use these same expressions.
_AGGREGATES = (
    "COUNT(*) AS requests, "
    "COALESCE(SUM(COALESCE(l.prompt_tokens, 0) + COALESCE(l.completion_tokens, 0)), 0) "
    "AS tokens, "
    "COALESCE(array_agg(DISTINCT l.user_id) FILTER (WHERE l.user_id IS NOT NULL), '{}') "
    "AS user_ids"
)

# Fingerprint of what a day stores; a day stored under any other is recounted.
DEFINITION = hashlib.sha256(_AGGREGATES.encode()).hexdigest()[:16]
_CURRENT = f"'{DEFINITION}'"


def _day_start(day_sql: str) -> str:
    return f"(({day_sql})::timestamp AT TIME ZONE 'UTC')"


# $1 first day, $2 last day (inclusive). One row per day with any stored or
# logged activity; a day with neither is absent.
_DAYS_SQL = f"""
WITH wanted AS (
    -- Stepped as naive timestamps: a timestamptz step is a calendar day in the
    -- session's time zone, 23 or 25 hours across a DST change.
    SELECT d::date AS day
    FROM generate_series($1::date::timestamp, $2::date::timestamp, interval '1 day') AS d
),
stored AS (
    SELECT a.day, a.requests, a.tokens, a.user_ids
    FROM daily_activity a
    JOIN wanted w ON w.day = a.day
    WHERE a.definition = {_CURRENT} AND a.day < {SETTLED_DAY_SQL}
),
runs AS (
    -- The wanted days not stored, as runs of consecutive days: one range scan
    -- each, joined LATERAL so the planner cannot turn them into a table scan.
    SELECT MIN(day) AS first_day, MAX(day) + 1 AS end_day
    FROM (
        SELECT day, day - (ROW_NUMBER() OVER (ORDER BY day))::int AS run
        FROM wanted
        WHERE day NOT IN (SELECT day FROM stored)
    ) ranked
    GROUP BY run
)
SELECT day, requests, tokens, user_ids FROM stored
UNION ALL
SELECT counted.*
FROM runs
CROSS JOIN LATERAL (
    SELECT (l.timestamp AT TIME ZONE 'UTC')::date AS day, {_AGGREGATES}
    FROM api_logs l
    WHERE l.timestamp >= {_day_start("runs.first_day")}
      AND l.timestamp < {_day_start("runs.end_day")}
    GROUP BY 1
) counted
"""

_FILL_DAY_SQL = f"""
INSERT INTO daily_activity AS a (day, requests, tokens, user_ids, definition, completed_at)
SELECT $1::date, counted.requests, counted.tokens, counted.user_ids, {_CURRENT}, NOW()
FROM (
    SELECT {_AGGREGATES}
    FROM api_logs l
    WHERE l.timestamp >= {_day_start("$1::date")}
      AND l.timestamp < {_day_start("$1::date + 1")}
) counted
ON CONFLICT (day) DO UPDATE SET
    requests = EXCLUDED.requests,
    tokens = EXCLUDED.tokens,
    user_ids = EXCLUDED.user_ids,
    definition = EXCLUDED.definition,
    completed_at = EXCLUDED.completed_at
"""

# The first day kept; earlier days are neither filled nor read from the table.
_FIRST_KEPT_DAY_SQL = f"({SETTLED_DAY_SQL} - {RETENTION_DAYS})"

# Whether *day* still needs filling, decided under the lock.
_DAY_DUE_SQL = f"""
SELECT $1::date < {SETTLED_DAY_SQL}
   AND $1::date >= {_FIRST_KEPT_DAY_SQL}
   AND NOT EXISTS (
       SELECT 1 FROM daily_activity WHERE day = $1::date AND definition = {_CURRENT}
   )
"""

# Kept days not stored under the current definition, newest first.
_MISSING_DAYS_SQL = f"""
SELECT d::date AS day
FROM generate_series(
    {_FIRST_KEPT_DAY_SQL}::timestamp, ({SETTLED_DAY_SQL} - 1)::timestamp, interval '1 day'
) AS d
WHERE NOT EXISTS (
    SELECT 1 FROM daily_activity a WHERE a.day = d::date AND a.definition = {_CURRENT}
)
ORDER BY 1 DESC
"""

_PURGE_SQL = f"DELETE FROM daily_activity WHERE day < {_FIRST_KEPT_DAY_SQL}"


async def load_days(
    conn: asyncpg.Connection, first_day: date, last_day: date
) -> list[dict[str, Any]]:
    """The growth series' days from *first_day* to *last_day*, inclusive.

    One dict per day, oldest first, quiet days included: ``day``,
    ``active_users``, ``new_users`` (first active day within this range),
    ``requests`` and ``tokens``.
    """
    async with conn.transaction():
        await conn.execute(NO_JIT_SQL)
        rows = await conn.fetch(_DAYS_SQL, first_day, last_day)
    by_day = {row["day"]: row for row in rows}

    first_seen: dict[str, date] = {}
    for day in sorted(by_day):
        for user_id in by_day[day]["user_ids"]:
            first_seen.setdefault(user_id, day)
    new_users: dict[date, int] = {}
    for day in first_seen.values():
        new_users[day] = new_users.get(day, 0) + 1

    days: list[dict[str, Any]] = []
    day = first_day
    while day <= last_day:
        row = by_day.get(day)
        days.append(
            {
                "day": day,
                "active_users": len(row["user_ids"]) if row else 0,
                "new_users": new_users.get(day, 0),
                "requests": int(row["requests"]) if row else 0,
                "tokens": int(row["tokens"]) if row else 0,
            }
        )
        day += timedelta(days=1)
    return days


async def fill_days(conn: asyncpg.Connection, days: Iterable[date]) -> int:
    """Count *days* into the table, one transaction each under the lock."""
    written = 0
    for day in days:
        async with conn.transaction():
            await conn.execute(NO_JIT_SQL)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
            if not await conn.fetchval(_DAY_DUE_SQL, day):
                continue
            await conn.execute(_FILL_DAY_SQL, day)
            written += 1
    return written


async def backfill_missing(pool: asyncpg.Pool) -> int:
    """Store every kept, completed day the table lacks, newest first."""
    async with pool.acquire() as conn:
        days = [row["day"] for row in await conn.fetch(_MISSING_DAYS_SQL)]
        return await fill_days(conn, days) if days else 0


async def invalidate_days(conn: asyncpg.Connection, days: list[date]) -> None:
    """Drop the stored *days*, in the caller's transaction.

    For any delete of ``api_logs`` rows, passed the days those rows were on, in
    the delete's transaction so the two commit or fail together. Under the lock,
    a fill either committed before this -- and is dropped here -- or starts after
    the delete commits and reads what is left. The days are counted live until
    the next fill.
    """
    if not days:
        return
    await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
    await conn.execute("DELETE FROM daily_activity WHERE day = ANY($1::date[])", days)


async def hourly_job(pool: asyncpg.Pool) -> None:
    """Store the day that completed, refill dropped days, and purge old ones."""
    started_at = time.monotonic()
    written = 0
    outcome = "error"
    try:
        written = await backfill_missing(pool)
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
            await conn.execute(_PURGE_SQL)
        outcome = "ok"
    except Exception as exc:
        logger.exception(f"rollup_daily_activity failed: {exc}")
    finally:
        logger.info(
            "rollup_daily_activity: days=%d duration_ms=%d outcome=%s",
            written,
            int((time.monotonic() - started_at) * 1000),
            outcome,
        )


def register_rollup_job(scheduler: Any, pool: asyncpg.Pool) -> None:
    """Register the hourly job on the existing AsyncIOScheduler (minute 25).

    Hourly, so a day a delete dropped is stored again within the hour. Minute 25
    stays clear of the provider (5), geo (10), request volume (15) and user
    usage (20) rollups.
    """
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        hourly_job,
        trigger=CronTrigger(minute=25, timezone=timezone.utc),
        args=[pool],
        id="rollup_daily_activity",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    logger.info("rollup_daily_activity: registered on scheduler (cron minute=25)")
