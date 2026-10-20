"""Daily per-user rollup behind the admin Users table's all-time columns.

The Users table shows each user's all-time request count, token total, average
turns, average user turns and ask-question share, and can sort by the first two.
All used to be computed straight off ``api_logs`` -- every row a user ever
logged, on every page load. On staging that was about 8.4 million rows for 30
users: the list took 8 to 38 seconds and sometimes hit the proxy's 30-second
limit. Yet only the newest day's rows can still change.

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

What a day stores is :data:`METRICS`, one SQL definition shared by the fill and
the live parts. Each is a count or a sum, so days add up: an average is stored
as its sum and count, a share as its two counts. They match the queries they
replace: every logged row with a ``user_id``, free and failed requests
included, missing token counts as zero, and averages over the rows that
recorded a turn count. ``user_daily_cost.requests`` is not a substitute: it
only counts billable requests.

A coverage row records the :data:`DEFINITION` it was counted under. A day
counted under any other -- before a metric was added, or before the ask-question
tool list changed -- is not stored as far as reads are concerned: it is counted
live and filled again.

Until the startup migration has added every column the stored reads name, reads
count straight from ``api_logs`` instead (:func:`stored_ready`). The migration
waits only briefly for its lock: behind a long holder such as the nightly
``pg_dump`` it is deferred and retried in the background, while requests are
already being served.

Days reach the table through :func:`fill_days`, called by the hourly job and a
backfill at startup. Every fill, invalidation and purge holds one
transaction-scoped advisory lock, and a fill re-reads whether its day is still
missing once it holds it.

Deletes are the one way a completed day changes. "Clear errors" invalidates the
days it deleted rows from, in the delete's own transaction
(:func:`invalidate_days`): were the two separate, a failed invalidation would
leave a day marked counted with the deleted rows in it, and nothing would ever
count it again. Unlike the anonymous request volume
rollup, these rows name a user, so a hard delete removes them in the same
transaction that purges the user's ``api_logs`` rows (:func:`purge_user`).
"""

from __future__ import annotations

import hashlib
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


# Ask-question adoption is measured over each user's *early-conversation*
# requests only -- those with ``num_turns <= ASK_QUESTION_TURN_CAP``. Agentic
# clients re-send the same tool manifest on every turn, so a single long
# conversation would otherwise dominate the per-request count; capping to the
# opening turns approximates one observation per conversation. The manifest is
# fixed within a conversation (an ask tool present at turn 1 is present
# throughout), so the opening turns are representative of the whole trajectory.
ASK_QUESTION_TURN_CAP = 5

# Tool names (lower-cased) that count as an ask-the-user clarifying tool, matched
# against both the OpenAI (``function.name``) and Anthropic (``name``) tool
# shape. Deliberately broad -- it spans the coding-agent clients we serve
# (e.g. Claude Code's ``AskUserQuestion``, plus ``question`` /
# ``ask_followup_question`` / ``vscode_askquestions`` and the ``ask_user_*``
# variants).
ASK_QUESTION_TOOL_NAMES: tuple[str, ...] = (
    "question",
    "askuserquestion",
    "ask_user_question",
    "ask_user_questions",
    "ask_followup_question",
    "vscode_askquestions",
)


def _text_array(values: tuple[str, ...]) -> str:
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"ARRAY[{quoted}]::text[]"


# What a day stores per user: ``(column, aggregate over api_logs rows as l)``.
# The fill and every live part of a read use these same expressions.
METRICS: tuple[tuple[str, str], ...] = (
    ("request_count", "COUNT(*)"),
    ("total_tokens", "COALESCE(SUM(l.total_tokens), 0)"),
    # AVG(num_turns) as a sum and a count: non-chat rows record no turn count.
    ("turns_sum", "COALESCE(SUM(l.num_turns), 0)"),
    ("turns_count", "COUNT(l.num_turns)"),
    ("user_turns_sum", "COALESCE(SUM(l.num_user_turns), 0)"),
    ("user_turns_count", "COUNT(l.num_user_turns)"),
    ("early_requests", f"COUNT(*) FILTER (WHERE l.num_turns <= {ASK_QUESTION_TURN_CAP})"),
    (
        "early_ask_requests",
        f"""COUNT(*) FILTER (
            WHERE l.num_turns <= {ASK_QUESTION_TURN_CAP}
              AND jsonb_typeof(l.tools) = 'array'
              AND EXISTS (
                  SELECT 1 FROM jsonb_array_elements(l.tools) AS t
                  WHERE lower(COALESCE(t->'function'->>'name', t->>'name'))
                        = ANY({_text_array(ASK_QUESTION_TOOL_NAMES)})
              )
        )""",
    ),
)
_COLUMNS = [column for column, _ in METRICS]
_AGGREGATES = ", ".join(expression for _, expression in METRICS)

# Fingerprint of :data:`METRICS`, recorded with each counted day. Any change to
# what a day stores changes it, and the days counted before are filled again.
DEFINITION = hashlib.sha256(repr(METRICS).encode()).hexdigest()[:16]
_CURRENT = f"'{DEFINITION}'"


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
    SELECT 1 FROM user_usage_daily_coverage c
    WHERE c.day = g.day::date AND c.definition = {_CURRENT}
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


# Every rollup column a stored read or fill names, all present once the startup
# migration has run (see :func:`stored_ready`).
_READY_SQL = """
SELECT count(*) FROM pg_attribute
WHERE attnum > 0 AND NOT attisdropped
  AND ((attrelid = to_regclass('user_usage_daily') AND attname = ANY($1::text[]))
    OR (attrelid = to_regclass('user_usage_daily_coverage') AND attname = 'definition'))
"""
READY_COLUMN_COUNT = len(METRICS) + 1


async def stored_ready(conn: asyncpg.Connection) -> bool:
    """Whether the tables have every column the stored reads and fills name.

    False only between an upgrade's start and its deferred column migration
    landing; reads then count from ``api_logs`` and fills wait. One catalog
    lookup: columns are only ever added, so a True answer cannot go stale
    before the statement it gates runs.
    """
    return await conn.fetchval(_READY_SQL, _COLUMNS) == READY_COLUMN_COUNT


def metrics_sql(user_match: str, *, stored: bool = True) -> str:
    """SQL for ``user_id`` and every :data:`METRICS` column, per matching user.

    *user_match* completes ``<column> {user_match}`` for the users wanted, e.g.
    ``"= ANY($1::text[])"`` or ``"IN (SELECT id FROM filtered_users)"``. Users
    with no logged rows are absent from the result. With ``stored=False`` --
    for when :func:`stored_ready` is not -- every row is counted from
    ``api_logs``, as before the rollup. Otherwise each live part is a range on
    ``(user_id, timestamp)``, which ``idx_api_logs_user_cost`` serves. The gap
    runs are joined ``LATERAL``, one bounded range scan per run: the planner
    cannot size the gap list (it guessed 500 days where there were none) and,
    joined plainly, answered with a scan of the whole table.
    """
    if not stored:
        named = ", ".join(f"{expression} AS {column}" for column, expression in METRICS)
        return f"""
SELECT l.user_id, {named}
FROM api_logs l
WHERE l.user_id {user_match}
GROUP BY l.user_id
"""
    stored_columns = ", ".join(f"d.{column}" for column in _COLUMNS)
    summed = ", ".join(f"SUM({column})::bigint AS {column}" for column in _COLUMNS)
    return f"""
SELECT user_id, {summed}
FROM (
    SELECT d.user_id, {stored_columns}
    FROM user_usage_daily d
    JOIN user_usage_daily_coverage c ON c.day = d.day AND c.definition = {_CURRENT}
    WHERE d.user_id {user_match} AND d.day < {SETTLED_DAY_SQL}
    UNION ALL
    SELECT l.user_id, {_AGGREGATES}
    FROM api_logs l
    WHERE l.user_id {user_match} AND l.timestamp >= {_day_start(SETTLED_DAY_SQL)}
    GROUP BY l.user_id
    UNION ALL
    SELECT l.user_id, {_AGGREGATES}
    FROM api_logs l
    WHERE l.user_id {user_match} AND l.timestamp IS NULL
    GROUP BY l.user_id
    UNION ALL
    SELECT counted.*
    FROM ({_GAP_RUNS_SQL}) gap
    CROSS JOIN LATERAL (
        SELECT l.user_id, {_AGGREGATES}
        FROM api_logs l
        WHERE l.user_id {user_match}
          AND l.timestamp >= {_day_start("gap.first_day")}
          AND l.timestamp < {_day_start("gap.end_day")}
        GROUP BY l.user_id
    ) counted
) parts
GROUP BY user_id
"""


_METRICS_FOR_IDS_SQL = metrics_sql("= ANY($1::text[])")
_LOGGED_METRICS_FOR_IDS_SQL = metrics_sql("= ANY($1::text[])", stored=False)

# Run inside the transaction of any statement using :func:`metrics_sql` or a
# fill. The planner's guess at the gap days, or at a day's rows, puts the cost
# far above what is read, enough to trigger JIT compilation -- seconds spent
# compiling scans that take milliseconds.
NO_JIT_SQL = "SET LOCAL jit = off"

_FILL_DAY_SQL = f"""
INSERT INTO user_usage_daily (user_id, day, {", ".join(_COLUMNS)})
SELECT l.user_id, $1::date, {_AGGREGATES}
FROM api_logs l
WHERE l.timestamp >= {_day_start("$1::date")}
  AND l.timestamp < {_day_start("$1::date + 1")}
  AND l.user_id IS NOT NULL
GROUP BY l.user_id
"""

_MARK_FILLED_SQL = f"""
INSERT INTO user_usage_daily_coverage (day, definition) VALUES ($1, {_CURRENT})
ON CONFLICT (day) DO UPDATE SET definition = EXCLUDED.definition, completed_at = NOW()
"""

# Whether *day* still needs filling, decided under the lock: not counted by
# another writer meanwhile, and still before the settled day.
_DAY_DUE_SQL = f"""
SELECT $1::date < {SETTLED_DAY_SQL}
   AND NOT EXISTS (
       SELECT 1 FROM user_usage_daily_coverage
       WHERE day = $1::date AND definition = {_CURRENT}
   )
"""


async def fetch_metrics(conn: asyncpg.Connection, user_ids: list[str]) -> dict[str, dict[str, int]]:
    """All-time :data:`METRICS` for each of *user_ids* with any logged row."""
    if not user_ids:
        return {}
    if not await stored_ready(conn):
        rows = await conn.fetch(_LOGGED_METRICS_FOR_IDS_SQL, user_ids)
    else:
        async with conn.transaction():
            await conn.execute(NO_JIT_SQL)
            rows = await conn.fetch(_METRICS_FOR_IDS_SQL, user_ids)
    return {row["user_id"]: {column: int(row[column]) for column in _COLUMNS} for row in rows}


async def fetch_totals(conn: asyncpg.Connection, user_ids: list[str]) -> dict[str, dict[str, int]]:
    """All-time ``{"requests", "tokens"}`` for each of *user_ids*, zeros included."""
    metrics = await fetch_metrics(conn, user_ids)
    return {
        uid: {
            "requests": metrics.get(uid, {}).get("request_count", 0),
            "tokens": metrics.get(uid, {}).get("total_tokens", 0),
        }
        for uid in user_ids
    }


def turn_averages(metrics: dict[str, int]) -> dict[str, float | None]:
    """``avg_turns`` / ``avg_user_turns`` from one user's :data:`METRICS`."""
    return {
        "avg_turns": (
            metrics["turns_sum"] / metrics["turns_count"] if metrics["turns_count"] else None
        ),
        "avg_user_turns": (
            metrics["user_turns_sum"] / metrics["user_turns_count"]
            if metrics["user_turns_count"]
            else None
        ),
    }


def ask_question_share(metrics: dict[str, int]) -> dict[str, float | int | None]:
    """The ask-question share from one user's :data:`METRICS`."""
    n_requests, n_ask = metrics["early_requests"], metrics["early_ask_requests"]
    return {
        "ask_question_fraction": (n_ask / n_requests) if n_requests else None,
        "n_requests": n_requests,
        "n_ask_requests": n_ask,
    }


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
            # Rows without a current coverage row are left over from an
            # invalidation or were counted under an earlier definition.
            await conn.execute("DELETE FROM user_usage_daily WHERE day = $1", day)
            await conn.execute(_FILL_DAY_SQL, day)
            await conn.execute(_MARK_FILLED_SQL, day)
            written += 1
    return written


async def backfill_missing(pool: asyncpg.Pool) -> int:
    """Store every completed day the table lacks, newest first.

    On the first start with the table that is the whole history; afterwards it
    is the day that just completed and any day a delete invalidated. Returns how
    many days it wrote.
    """
    async with pool.acquire() as conn:
        if not await stored_ready(conn):
            logger.info("rollup_user_usage: columns not migrated yet, nothing filled")
            return 0
        days = [row["day"] for row in await conn.fetch(_GAP_DAYS_SQL + " ORDER BY day DESC")]
        if not days:
            return 0
        return await fill_days(conn, days)


async def invalidate_days(conn: asyncpg.Connection, days: list[date]) -> None:
    """Drop the stored *days*, in the caller's transaction.

    For a delete of ``api_logs`` rows, in the delete's transaction, so the two
    commit or fail together. Under the advisory lock, a fill either committed
    before this -- and whatever it read from before the delete is dropped here --
    or starts after the delete commits and reads the rows as they are then. The
    dropped days are counted live until the next fill.
    """
    if not days:
        return
    await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
    await conn.execute("DELETE FROM user_usage_daily_coverage WHERE day = ANY($1::date[])", days)
    await conn.execute("DELETE FROM user_usage_daily WHERE day = ANY($1::date[])", days)


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
