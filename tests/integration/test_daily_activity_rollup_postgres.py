"""PostgreSQL integration tests for the growth series' daily rollup.

``/admin/analytics/growth`` used to group every ``api_logs`` row of the last 30,
60 or 90 days by (day, user) on each load. It now reads completed days from
``daily_activity`` and counts the rest live (``daily_activity_rollup``). These
run the real SQL and hold every read to the query it replaced -- kept here
verbatim as the oracle -- in each state the table can be in, over rows on each
seam: day boundaries, anonymous and audit-only rows, missing token counts,
users active before the range, and the settled day.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from serving.storage import daily_activity_rollup, user_usage_rollup
from serving.storage.log_schema import ensure_api_logs_schema
from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

# What /admin/analytics/growth ran before the rollup, verbatim. $1 the first
# day's midnight, $2 the last (today's), $3 the midnight after it.
_ORACLE_SQL = """
WITH series AS (
    -- Step in naive UTC, not timestamptz: `timestamptz + interval
    -- '1 day'` is a calendar day in the *session* timezone, so a
    -- range crossing a DST boundary steps 23 or 25 hours and the
    -- buckets drift off UTC midnight, matching nothing in the join
    -- and zeroing out real days.
    SELECT d AT TIME ZONE 'UTC' AS day
    FROM generate_series(
        $1::timestamptz AT TIME ZONE 'UTC',
        $2::timestamptz AT TIME ZONE 'UTC',
        interval '1 day'
    ) AS d
),
per_user_day AS (
    SELECT
        (date_trunc('day', l.timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS day,
        l.user_id,
        COUNT(*) AS requests,
        COALESCE(
            SUM(COALESCE(l.prompt_tokens, 0) + COALESCE(l.completion_tokens, 0)),
            0
        ) AS tokens
    FROM api_logs l
    WHERE l.timestamp >= $1::timestamptz
      AND l.timestamp < $3::timestamptz
    GROUP BY 1, 2
),
daily AS (
    -- Rows are already unique per (day, user), so the FILTER count
    -- is the day's distinct signed-in users. SUM(bigint) comes back
    -- numeric from asyncpg; cast so the ints stay ints.
    SELECT
        day,
        COUNT(*) FILTER (WHERE user_id IS NOT NULL) AS active_users,
        SUM(requests)::bigint AS requests,
        SUM(tokens)::bigint AS tokens
    FROM per_user_day
    GROUP BY day
),
new_users AS (
    SELECT first_day AS day, COUNT(*)::bigint AS new_users
    FROM (
        SELECT user_id, MIN(day) AS first_day
        FROM per_user_day
        WHERE user_id IS NOT NULL
        GROUP BY user_id
    ) f
    GROUP BY first_day
)
SELECT
    s.day AS day,
    COALESCE(d.active_users, 0) AS active_users,
    COALESCE(n.new_users, 0) AS new_users,
    COALESCE(d.tokens, 0) AS tokens,
    COALESCE(d.requests, 0) AS requests
FROM series s
LEFT JOIN daily d ON d.day = s.day
LEFT JOIN new_users n ON n.day = s.day
ORDER BY s.day ASC
"""

_FENCE_SECRET = "test-fence-secret-not-for-production"


@pytest_asyncio.fixture
async def db():
    schema = "daily_activity_" + uuid.uuid4().hex
    admin = await postgres.connect()
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await postgres.create_pool(
        min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            await ensure_api_logs_schema(conn)
            settled = await conn.fetchval(f"SELECT {user_usage_rollup.SETTLED_DAY_SQL}")
            today = await conn.fetchval("SELECT (NOW() AT TIME ZONE 'UTC')::date")
        yield pool, PostgresLogStore(pool, fence_secret=_FENCE_SECRET), settled, today
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _midnight(day) -> datetime:
    return datetime(day.year, day.month, day.day)


async def _log(conn, user_id, at, prompt=100, completion=10, *, status=200, error=None, owner=None):
    """One row; *at* a naive UTC timestamp. *owner* names an account in metadata only."""
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, user_id, timestamp, "
        "prompt_tokens, completion_tokens, status_code, error, metadata) "
        "VALUES ($1, 'm', 'p', $2, $3::timestamp AT TIME ZONE 'UTC', $4, $5, $6, $7, $8::jsonb)",
        uuid.uuid4().hex,
        user_id,
        at,
        prompt,
        completion,
        status,
        error,
        json.dumps({"credential_owner_id": owner}) if owner else None,
    )


async def _seed(pool, settled) -> None:
    s = _midnight(settled)
    us = timedelta(microseconds=1)
    day = timedelta(days=1)
    async with pool.acquire() as conn:
        rows = [
            ("early", s - 80 * day + timedelta(hours=5), 100, 10),  # new in a 90-day range
            ("early", s - 10 * day, 100, 10),  # but not new in a 30-day one
            ("heavy", s - 89 * day, 7, 3),  # first day of a 90-day range
            ("heavy", s - 3 * day - us, 1, 1),  # last instant of a day
            ("heavy", s - 3 * day, 2, 2),  # first instant of the next
            ("heavy", s - 3 * day + timedelta(hours=1), None, None),  # no token counts
            ("heavy", s - us, 50, 5),  # last instant before the settled day
            ("heavy", s + timedelta(minutes=1), 40, 4),  # counted live
            ("light", s - 29 * day, 9, 9),
            ("light", s - 2 * day, None, 8),
            (None, s - 3 * day, 1000, 1000),  # anonymous: requests and tokens only
            (None, s + timedelta(minutes=2), 500, 0),
            ("erased", s - 40 * day, 30, 3),
            ("erased", s - 5 * day, 30, 3),
            ("ancient", s - 120 * day, 1, 1),  # older than any range
        ]
        for user_id, at, prompt, completion in rows:
            await _log(conn, user_id, at, prompt, completion)
        # An audit-only row naming "erased" in metadata, as a rejected key does.
        await _log(conn, None, s - 6 * day, 5, 0, status=401, error="bad key", owner="erased")


def _oracle_day(row):
    return {
        "day": row["day"].astimezone(timezone.utc).date(),
        "active_users": int(row["active_users"]),
        "new_users": int(row["new_users"]),
        "requests": int(row["requests"]),
        "tokens": int(row["tokens"]),
    }


async def _assert_matches_oracle(pool, today) -> None:
    async with pool.acquire() as conn:
        for days in (30, 60, 90):
            first = today - timedelta(days=days - 1)
            end = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
            start = end - timedelta(days=days - 1)
            oracle = [
                _oracle_day(r)
                for r in await conn.fetch(_ORACLE_SQL, start, end, end + timedelta(days=1))
            ]
            got = await daily_activity_rollup.load_days(conn, first, today)
            assert got == oracle, f"{days}-day series differs"


async def _stored(pool) -> dict:
    async with pool.acquire() as conn:
        return {
            r["day"]: r
            for r in await conn.fetch(
                "SELECT * FROM daily_activity WHERE definition = $1",
                daily_activity_rollup.DEFINITION,
            )
        }


async def test_series_matches_the_full_scan_before_and_after_the_backfill(db):
    pool, _, settled, today = db
    await _seed(pool, settled)
    await _assert_matches_oracle(pool, today)

    written = await daily_activity_rollup.backfill_missing(pool)
    stored = await _stored(pool)
    # Every kept day before the settled one, quiet days included.
    assert written == len(stored) == daily_activity_rollup.RETENTION_DAYS
    assert min(stored) == settled - timedelta(days=daily_activity_rollup.RETENTION_DAYS)
    assert max(stored) == settled - timedelta(days=1)
    assert await daily_activity_rollup.backfill_missing(pool) == 0
    await _assert_matches_oracle(pool, today)

    # Boundary rows land on their own UTC day; anonymous rows add requests and
    # tokens but no user; missing token counts add nothing.
    before = stored[settled - timedelta(days=4)]
    assert (before["requests"], before["tokens"], before["user_ids"]) == (1, 2, ["heavy"])
    day = stored[settled - timedelta(days=3)]
    assert (day["requests"], day["tokens"], day["user_ids"]) == (3, 2004, ["heavy"])
    assert stored[settled - timedelta(days=7)]["user_ids"] == []


async def test_dropped_or_redefined_days_are_counted_live_then_refilled(db):
    pool, _, settled, today = db
    await _seed(pool, settled)
    await daily_activity_rollup.backfill_missing(pool)

    dropped = settled - timedelta(days=10)
    redefined = [settled - timedelta(days=3), settled - timedelta(days=4)]
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM daily_activity WHERE day = $1", dropped)
        # Wrong numbers under a retired definition: neither read nor added.
        await conn.execute(
            "UPDATE daily_activity SET definition = 'retired', requests = 999, "
            "user_ids = '{ghost}' WHERE day = ANY($1::date[])",
            redefined,
        )
    await _assert_matches_oracle(pool, today)

    assert await daily_activity_rollup.backfill_missing(pool) == 3
    await _assert_matches_oracle(pool, today)


async def test_clear_errors_drops_exactly_the_days_it_deleted_from(db):
    pool, log_store, settled, today = db
    await _seed(pool, settled)
    error_at = _midnight(settled) - timedelta(hours=1)
    async with pool.acquire() as conn:
        await _log(conn, "light", error_at, 77, 7, status=500, error="boom")
    await daily_activity_rollup.backfill_missing(pool)
    stored = set(await _stored(pool))

    assert await log_store.delete_recent_error_requests(hours=48) == 1
    assert set(await _stored(pool)) == stored - {error_at.date()}
    await _assert_matches_oracle(pool, today)
    assert await daily_activity_rollup.backfill_missing(pool) == 1
    await _assert_matches_oracle(pool, today)


async def test_hard_delete_drops_every_day_naming_the_user(db):
    pool, log_store, settled, today = db
    await _seed(pool, settled)
    await daily_activity_rollup.backfill_missing(pool)

    counts = await log_store.hard_delete_user_data("erased")

    assert counts["api_logs"] == 3  # two of theirs, one audit-only row naming them
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM daily_activity WHERE 'erased' = ANY(user_ids)"
            )
            == 0
        )
    gone = {settled - timedelta(days=d) for d in (40, 6, 5)}
    assert not gone & set(await _stored(pool))
    await _assert_matches_oracle(pool, today)

    assert await daily_activity_rollup.backfill_missing(pool) == 3
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM daily_activity WHERE 'erased' = ANY(user_ids)"
            )
            == 0
        )
    await _assert_matches_oracle(pool, today)


async def test_a_fill_waits_for_a_delete_holding_the_lock(db):
    """A fill that starts while a delete holds the lock reads what is left."""
    pool, _, settled, _ = db
    await _seed(pool, settled)
    day = settled - timedelta(days=5)

    deleting = await pool.acquire()
    filler = await pool.acquire()
    try:
        tx = deleting.transaction()
        await tx.start()
        await deleting.execute("DELETE FROM api_logs WHERE user_id = 'erased'")
        await daily_activity_rollup.invalidate_days(deleting, [day])

        fill = asyncio.create_task(daily_activity_rollup.fill_days(filler, [day]))
        await asyncio.sleep(0.3)
        assert not fill.done()  # blocked on the lock the delete holds
        await tx.commit()
        assert await fill == 1
    finally:
        await pool.release(deleting)
        await pool.release(filler)

    assert "erased" not in (await _stored(pool))[day]["user_ids"]


async def test_the_hourly_job_purges_days_older_than_kept(db):
    pool, _, settled, _ = db
    await _seed(pool, settled)
    old = settled - timedelta(days=daily_activity_rollup.RETENTION_DAYS + 30)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO daily_activity (day, requests, tokens, user_ids, definition) "
            "VALUES ($1, 1, 1, '{someone}', $2)",
            old,
            daily_activity_rollup.DEFINITION,
        )
    await daily_activity_rollup.hourly_job(pool)
    stored = await _stored(pool)
    assert old not in stored
    assert len(stored) == daily_activity_rollup.RETENTION_DAYS
