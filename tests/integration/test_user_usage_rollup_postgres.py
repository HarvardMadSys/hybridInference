"""PostgreSQL integration tests for the daily per-user usage rollup.

The admin Users table's all-time request and token totals used to be summed
straight off ``api_logs``. They now read completed days from
``user_usage_daily`` and count the rest live (see
``serving/storage/user_usage_rollup.py``). These run the real SQL and hold every
read to the query it replaced -- kept here verbatim as the oracle -- over rows
placed on each seam: day boundaries, the settled day, rows without a timestamp,
user or token count, and days the rollup has not stored or has dropped.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage import user_usage_rollup
from serving.storage.log_schema import ensure_api_logs_schema
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

# What the Users table ran before the rollup, verbatim.
_ORACLE_SQL = (
    "SELECT user_id, COUNT(*) AS requests, "
    "COALESCE(SUM(total_tokens), 0) AS tokens FROM api_logs "
    "WHERE user_id = ANY($1::text[]) GROUP BY user_id"
)

_USERS = ["heavy", "light", "quiet", "erased"]
_FENCE_SECRET = "test-fence-secret-not-for-production"


@pytest_asyncio.fixture
async def db():
    """An isolated schema with the log tables, users, and both stores over it."""
    schema = "usage_rollup_" + uuid.uuid4().hex
    admin = await postgres.connect()
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await postgres.create_pool(
        min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            for table in ("users", "api_keys", "user_daily_cost"):
                await conn.execute(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
            await ensure_api_logs_schema(conn)
            for i, uid in enumerate(_USERS):
                await conn.execute(
                    "INSERT INTO users (id, email, password_hash, status, created_at) "
                    "VALUES ($1, $1 || '@example.test', 'fixture', 'active', $2)",
                    uid,
                    datetime(2026, 1, 1 + i),
                )
            settled = await conn.fetchval(f"SELECT {user_usage_rollup.SETTLED_DAY_SQL}")
        log_store = PostgresLogStore(pool, fence_secret=_FENCE_SECRET)
        yield pool, PostgresOperationalStore(pool), log_store, settled
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _midnight(day) -> datetime:
    """UTC midnight starting *day*, as a naive timestamp for ``AT TIME ZONE``."""
    return datetime(day.year, day.month, day.day)


async def _log(conn, user_id, at, tokens, *, status=200, error=None) -> None:
    """Insert one row; *at* is a naive UTC timestamp, or None for no timestamp."""
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, user_id, timestamp, "
        "total_tokens, cost_usd, status_code, error) "
        "VALUES ($1, 'm', 'p', $2, $3::timestamp AT TIME ZONE 'UTC', $4, 0, $5, $6)",
        uuid.uuid4().hex,
        user_id,
        at,
        tokens,
        status,
        error,
    )


async def _seed(pool, settled) -> None:
    """Rows on every seam, relative to the first day still counted live."""
    s = _midnight(settled)
    us = timedelta(microseconds=1)
    async with pool.acquire() as conn:
        rows = [
            ("heavy", s - timedelta(days=40) + timedelta(hours=3), 100),
            ("heavy", s - timedelta(days=3) - us, 7),  # last instant of a day
            ("heavy", s - timedelta(days=3), 11),  # first instant of the next
            ("heavy", s - timedelta(days=1) + timedelta(hours=23), None),  # no tokens
            ("heavy", s - us, 5),  # last instant before the settled day
            ("heavy", s, 13),  # first live instant
            ("heavy", s + timedelta(minutes=5), 17),
            ("heavy", None, 19),  # no timestamp
            ("light", s - timedelta(days=12), 0),  # free, zero tokens
            ("light", s - timedelta(days=2) + timedelta(hours=6), 3),
            ("erased", s - timedelta(days=5), 1000),
            ("erased", s + timedelta(minutes=1), 1),
            (None, s - timedelta(days=3), 50_000),  # no user: never counted
        ]
        for user_id, at, tokens in rows:
            await _log(conn, user_id, at, tokens)


async def _assert_matches_oracle(pool, op_store) -> None:
    async with pool.acquire() as conn:
        oracle = {uid: {"requests": 0, "tokens": 0} for uid in _USERS}
        for row in await conn.fetch(_ORACLE_SQL, _USERS):
            oracle[row["user_id"]] = {"requests": row["requests"], "tokens": row["tokens"]}
    assert await op_store.get_bulk_user_usage_totals(_USERS) == oracle

    _, rows, _ = await op_store.list_users()
    assert {r["id"]: (r["usage_alltime_requests"], r["usage_alltime_tokens"]) for r in rows} == {
        uid: (t["requests"], t["tokens"]) for uid, t in oracle.items()
    }
    for sort_by in ("requests", "tokens"):
        _, rows, _ = await op_store.list_users(sort_by=sort_by, include_usage_totals=False)
        # Ties fall back to the newest account first; later _USERS are newer.
        expected = sorted(_USERS, key=lambda uid: (-oracle[uid][sort_by], -_USERS.index(uid)))
        assert [r["id"] for r in rows] == expected
        assert all(
            (r["usage_alltime_requests"], r["usage_alltime_tokens"])
            == (oracle[r["id"]]["requests"], oracle[r["id"]]["tokens"])
            for r in rows
        )


async def _covered(pool) -> list:
    async with pool.acquire() as conn:
        return [r["day"] for r in await conn.fetch("SELECT day FROM user_usage_daily_coverage")]


async def test_totals_match_the_full_scan_before_and_after_the_backfill(db):
    pool, op_store, _, settled = db
    await _seed(pool, settled)

    # Nothing stored yet: every day is a gap and counted live.
    assert await _covered(pool) == []
    await _assert_matches_oracle(pool, op_store)

    written = await user_usage_rollup.backfill_missing(pool)
    covered = await _covered(pool)
    # Every day from the first logged one up to, not including, the settled day.
    assert written == len(covered) == 40
    assert min(covered) == settled - timedelta(days=40)
    assert max(covered) == settled - timedelta(days=1)
    await _assert_matches_oracle(pool, op_store)

    # A second run finds nothing to do.
    assert await user_usage_rollup.backfill_missing(pool) == 0

    async with pool.acquire() as conn:
        stored = {
            (r["user_id"], r["day"]): (r["request_count"], r["total_tokens"])
            for r in await conn.fetch("SELECT * FROM user_usage_daily")
        }
    # Boundary rows land on their own UTC day; a row without tokens adds none.
    assert stored[("heavy", settled - timedelta(days=4))] == (1, 7)
    assert stored[("heavy", settled - timedelta(days=3))] == (1, 11)
    assert stored[("heavy", settled - timedelta(days=1))] == (2, 5)
    assert all(user_id is not None for user_id, _ in stored)


async def test_a_dropped_day_is_counted_live_until_it_is_filled_again(db):
    pool, op_store, _, settled = db
    await _seed(pool, settled)
    await user_usage_rollup.backfill_missing(pool)

    dropped = settled - timedelta(days=3)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM user_usage_daily_coverage WHERE day = $1", dropped)
        # A row without coverage is ignored, not added on top of the live count.
        await conn.execute(
            "UPDATE user_usage_daily SET request_count = 999 WHERE day = $1", dropped
        )
    await _assert_matches_oracle(pool, op_store)

    assert await user_usage_rollup.backfill_missing(pool) == 1
    await _assert_matches_oracle(pool, op_store)


async def test_clear_errors_invalidates_the_days_it_reached(db):
    pool, op_store, log_store, settled = db
    await _seed(pool, settled)
    async with pool.acquire() as conn:
        now = await conn.fetchval("SELECT (NOW() AT TIME ZONE 'UTC')")
        await _log(conn, "light", now - timedelta(hours=2), 40, status=500, error="boom")
    await user_usage_rollup.backfill_missing(pool)

    assert await log_store.delete_recent_error_requests(hours=24) == 1
    await user_usage_rollup.invalidate_recent(pool, 24)
    await _assert_matches_oracle(pool, op_store)
    dropped = {settled - timedelta(days=d) for d in range(1, 41)} - set(await _covered(pool))
    assert dropped
    assert min(dropped) >= (now - timedelta(hours=25)).date()

    await user_usage_rollup.backfill_missing(pool)
    await _assert_matches_oracle(pool, op_store)


async def test_hard_delete_purges_the_users_stored_days(db):
    pool, op_store, log_store, settled = db
    await _seed(pool, settled)
    await user_usage_rollup.backfill_missing(pool)
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM user_usage_daily WHERE user_id = 'erased'")
            == 1
        )

    counts = await log_store.hard_delete_user_data("erased")

    assert counts["api_logs"] == 2
    assert counts["user_usage_daily"] == 1
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM user_usage_daily WHERE user_id = 'erased'")
            == 0
        )
        # Other users' days and the coverage stay.
        assert (
            await conn.fetchval("SELECT count(*) FROM user_usage_daily WHERE user_id = 'heavy'") > 0
        )
    assert len(await _covered(pool)) == 40
    await _assert_matches_oracle(pool, op_store)


async def test_a_fill_waits_for_a_hard_delete_holding_the_lock(db):
    """A fill that starts while a purge holds the lock reads the purged rows."""
    pool, _, _, settled = db
    await _seed(pool, settled)
    day = settled - timedelta(days=5)  # the erased user's stored day

    purge = await pool.acquire()
    filler = await pool.acquire()
    try:
        tx = purge.transaction()
        await tx.start()
        await purge.execute("DELETE FROM api_logs WHERE user_id = 'erased'")
        await user_usage_rollup.purge_user(purge, "erased")

        fill = asyncio.create_task(user_usage_rollup.fill_days(filler, [day]))
        await asyncio.sleep(0.3)
        assert not fill.done()  # blocked on the lock the purge holds
        await tx.commit()
        assert await fill == 1
    finally:
        await pool.release(purge)
        await pool.release(filler)

    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM user_usage_daily WHERE user_id = 'erased'")
            == 0
        )
