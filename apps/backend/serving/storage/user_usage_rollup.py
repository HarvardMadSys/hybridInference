"""Daily per-user rollup behind the admin Users table's all-time usage columns.

The Users table shows each user's all-time request count and token total, and can
sort by either. Both used to be summed straight off ``api_logs`` -- every row a
user ever logged, on every page load. On staging that was about 8.4 million rows
for 30 users: the list took 8 to 38 seconds and sometimes hit the proxy's
30-second limit. Yet only the newest day's rows can still change.

So completed UTC days are counted once into ``user_usage_daily`` (one row per
user with traffic that day) and ``user_usage_daily_coverage`` (one row per
counted day, quiet days included), both created with ``api_logs`` in
``serving.storage.log_schema``. A total is the stored days plus what is still
counted from ``api_logs`` on each read:

- the days from :data:`SETTLED_DAY_SQL` on, normally just today;
- rows with no timestamp;
- any earlier day with no coverage row: not yet filled, or invalidated by a
  delete. Such a day is counted live rather than left out, so a total never
  depends on whether the job has run.

All three parts are read in one statement, so they come from one snapshot: a
day a fill or an invalidation moves between the stored and live parts mid-read
is never counted twice or missed.

The counts match the ones they replace: every logged row with a ``user_id``,
free and failed requests included, and ``SUM(total_tokens)`` with missing
token counts as zero. ``user_daily_cost.requests`` is not a substitute: it only
counts billable requests.

Days reach the table through :func:`fill_days`, called by the hourly job and a
backfill at startup. Every fill, invalidation and purge holds one
transaction-scoped advisory lock, and a fill re-reads whether its day is still
missing once it holds it.

Deletes are the one way a completed day changes. "Clear errors" invalidates the
days it reached (:func:`invalidate_recent`). Unlike the anonymous request volume
rollup, these rows name a user, so a hard delete removes them in the same
transaction that purges the user's ``api_logs`` rows (:func:`purge_user`).
"""

from __future__ import annotations

import time
from datetime import timezone
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    import asyncpg

logger = get_logger(__name__)

# Held by every write to the two tables (see the module docstring). A constant
# 64-bit key, so writers on every replica serialize on the same lock.
ADVISORY_LOCK_KEY = 0x757372757367  # ascii "usrusg" packed

# The first day still counted live. A row's timestamp is taken when its log
# write starts, and the write can commit a little later, so a day is stored only
# once it has been over for an hour.
SETTLED_DAY_SQL = "(date_trunc('day', (NOW() - interval '1 hour') AT TIME ZONE 'UTC'))::date"


def _day_start(day_sql: str) -> str:
    """The UTC midnight starting *day_sql* (a ``date`` expression), as timestamptz."""
    return f"(({day_sql})::timestamp AT TIME ZONE 'UTC')"


# Days before the settled one that have no coverage row, from the first logged
# day on. ``MIN(timestamp)`` is one probe of ``idx_api_logs_timestamp``.
_GAP_DAYS_SQL = f"""
SELECT g.day::date AS day
FROM generate_series(
    (SELECT (MIN(timestamp) AT TIME ZONE 'UTC')::date FROM api_logs)::timestamp,
    ({SETTLED_DAY_SQL} - 1)::timestamp,
    interval '1 day'
) AS g(day)
WHERE NOT EXISTS (
    SELECT 1 FROM user_usage_daily_coverage c WHERE c.day = g.day::date
)
"""


# The gap days as runs of consecutive days, ``[first_day, end_day)``: a day
# minus its rank is the same for every day of one run. Before the first backfill
# every day is a gap, and one range per user reads far faster than a range per
# day.
_GAP_RUNS_SQL = f"""
SELECT MIN(day) AS first_day, MAX(day) + 1 AS end_day
FROM (
    SELECT day, day - (ROW_NUMBER() OVER (ORDER BY day))::int AS run
    FROM ({_GAP_DAYS_SQL}) gap_days
) ranked
GROUP BY run
"""


def totals_sql(user_match: str) -> str:
    """SQL for ``(user_id, requests, tokens)`` of every user matching *user_match*.

    *user_match* completes ``<column> {user_match}`` for the users wanted, e.g.
    ``"= ANY($1::text[])"`` or ``"IN (SELECT id FROM filtered_users)"``. Users
    with no logged rows are absent from the result. Each live part is a range on
    ``(user_id, timestamp)``, which ``idx_api_logs_user_cost`` serves. The gap
    runs are joined ``LATERAL``, one bounded range scan per run: the planner
    cannot size the gap list (it guessed 500 days where there were none) and,
    joined plainly, answered with a scan of the whole table.
    """
    return f"""
SELECT user_id, SUM(requests)::bigint AS requests, SUM(tokens)::bigint AS tokens
FROM (
    SELECT d.user_id, d.request_count AS requests, d.total_tokens AS tokens
    FROM user_usage_daily d
    JOIN user_usage_daily_coverage c ON c.day = d.day
    WHERE d.user_id {user_match} AND d.day < {SETTLED_DAY_SQL}
    UNION ALL
    SELECT l.user_id, COUNT(*), COALESCE(SUM(l.total_tokens), 0)
    FROM api_logs l
    WHERE l.user_id {user_match} AND l.timestamp >= {_day_start(SETTLED_DAY_SQL)}
    GROUP BY l.user_id
    UNION ALL
    SELECT l.user_id, COUNT(*), COALESCE(SUM(l.total_tokens), 0)
    FROM api_logs l
    WHERE l.user_id {user_match} AND l.timestamp IS NULL
    GROUP BY l.user_id
    UNION ALL
    SELECT counted.*
    FROM ({_GAP_RUNS_SQL}) gap
    CROSS JOIN LATERAL (
        SELECT l.user_id, COUNT(*), COALESCE(SUM(l.total_tokens), 0)
        FROM api_logs l
        WHERE l.user_id {user_match}
          AND l.timestamp >= {_day_start("gap.first_day")}
          AND l.timestamp < {_day_start("gap.end_day")}
        GROUP BY l.user_id
    ) counted
) parts
GROUP BY user_id
"""


_TOTALS_FOR_IDS_SQL = totals_sql("= ANY($1::text[])")

# Run inside the transaction of any statement using :func:`totals_sql` or a
# fill. The planner's guess at the gap days, or at a day's rows, puts the cost
# far above what is read, enough to trigger JIT compilation -- seconds spent
# compiling scans that take milliseconds.
NO_JIT_SQL = "SET LOCAL jit = off"

_FILL_DAY_SQL = f"""
INSERT INTO user_usage_daily (user_id, day, request_count, total_tokens)
SELECT user_id, $1::date, COUNT(*), COALESCE(SUM(total_tokens), 0)
FROM api_logs
WHERE timestamp >= {_day_start("$1::date")}
  AND timestamp < {_day_start("$1::date + 1")}
  AND user_id IS NOT NULL
GROUP BY user_id
"""

# Whether *day* still needs filling, decided under the lock: not counted by
# another writer meanwhile, and still before the settled day.
_DAY_DUE_SQL = f"""
SELECT $1::date < {SETTLED_DAY_SQL}
   AND NOT EXISTS (SELECT 1 FROM user_usage_daily_coverage WHERE day = $1::date)
"""

# From the day holding "now minus N hours", with an hour's margin: the delete
# this follows took its own "now" a moment earlier, which can fall a day back.
_INVALIDATE_FROM_SQL = (
    "((NOW() - make_interval(hours => $1::int) - interval '1 hour') AT TIME ZONE 'UTC')::date"
)


async def fetch_totals(conn: asyncpg.Connection, user_ids: list[str]) -> dict[str, dict[str, int]]:
    """All-time ``{"requests", "tokens"}`` for each of *user_ids*, zeros included."""
    totals = {uid: {"requests": 0, "tokens": 0} for uid in user_ids}
    if not user_ids:
        return totals
    async with conn.transaction():
        await conn.execute(NO_JIT_SQL)
        rows = await conn.fetch(_TOTALS_FOR_IDS_SQL, user_ids)
    for row in rows:
        totals[row["user_id"]] = {"requests": int(row["requests"]), "tokens": int(row["tokens"])}
    return totals


async def fill_days(conn: asyncpg.Connection, days: Iterable[date]) -> int:
    """Count *days* into the table, skipping any another writer stored meanwhile.

    Each day is its own transaction under the advisory lock, so the lock is never
    held across more than one day's rows. Returns how many days this call wrote.
    """
    written = 0
    for day in days:
        async with conn.transaction():
            await conn.execute(NO_JIT_SQL)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
            if not await conn.fetchval(_DAY_DUE_SQL, day):
                continue
            # Rows without a coverage row are left over from an invalidation.
            await conn.execute("DELETE FROM user_usage_daily WHERE day = $1", day)
            await conn.execute(_FILL_DAY_SQL, day)
            await conn.execute("INSERT INTO user_usage_daily_coverage (day) VALUES ($1)", day)
            written += 1
    return written


async def backfill_missing(pool: asyncpg.Pool) -> int:
    """Store every completed day the table lacks, newest first.

    On the first start with the table that is the whole history; afterwards it
    is the day that just completed and any day a delete invalidated. Returns how
    many days it wrote.
    """
    async with pool.acquire() as conn:
        days = [row["day"] for row in await conn.fetch(_GAP_DAYS_SQL + " ORDER BY day DESC")]
        if not days:
            return 0
        return await fill_days(conn, days)


async def invalidate_recent(pool: asyncpg.Pool, hours: int) -> None:
    """Drop the stored days a delete of the last *hours* hours of rows reached.

    Call it once the delete has committed. Under the advisory lock, a fill either
    finished before this -- and whatever it read from before the delete is
    dropped here -- or starts after it and reads the rows as they are now. The
    dropped days are counted live until the next fill.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
        await conn.execute(
            f"DELETE FROM user_usage_daily_coverage WHERE day >= {_INVALIDATE_FROM_SQL}", hours
        )
        await conn.execute(
            f"DELETE FROM user_usage_daily WHERE day >= {_INVALIDATE_FROM_SQL}", hours
        )


async def purge_user(conn: asyncpg.Connection, user_id: str) -> int:
    """Delete *user_id*'s stored days, in the caller's transaction.

    For the hard delete, which purges the user's ``api_logs`` rows in the same
    transaction. The lock orders this against a fill: one that read those rows
    before the purge committed its days first, and they are deleted here; one
    that starts after waits for the purge to commit and finds no rows. Coverage
    stays, as the days' counts no longer include the user. Returns the row count.
    """
    await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
    status = await conn.execute("DELETE FROM user_usage_daily WHERE user_id = $1", user_id)
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def hourly_job(pool: asyncpg.Pool) -> None:
    """Store the days that completed since the last run, and refill invalidated ones."""
    started_at = time.monotonic()
    written = 0
    outcome = "error"
    try:
        written = await backfill_missing(pool)
        outcome = "ok"
    except Exception as exc:
        logger.exception(f"rollup_user_usage failed: {exc}")
    finally:
        logger.info(
            "rollup_user_usage: days=%d duration_ms=%d outcome=%s",
            written,
            int((time.monotonic() - started_at) * 1000),
            outcome,
        )


def register_rollup_job(scheduler: Any, pool: asyncpg.Pool) -> None:
    """Register the hourly job on the existing AsyncIOScheduler.

    Hourly rather than daily, so a day an admin's "Clear errors" invalidated is
    stored again within the hour. A run with nothing to do is one gap query.
    Minute 20 stays clear of the provider (5), geo (10) and request volume (15)
    rollups.
    """
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        hourly_job,
        trigger=CronTrigger(minute=20, timezone=timezone.utc),
        args=[pool],
        id="rollup_user_usage",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    logger.info("rollup_user_usage: registered on scheduler (cron minute=20)")
