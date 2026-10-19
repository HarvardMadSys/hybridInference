"""PostgreSQL integration tests for the daily per-user usage rollup.

The admin Users table's all-time request and token totals, turn averages and
ask-question share used to be computed straight off ``api_logs``. They now read
completed days from ``user_usage_daily`` and count the rest live (see
``serving/storage/user_usage_rollup.py``). These run the real SQL and hold every
read to the queries they replaced -- kept here verbatim as oracles -- over rows
placed on each seam: day boundaries, the settled day, rows without a timestamp,
user, token or turn count, tool lists in each shape, and days the rollup has not
stored, has dropped, or counted under an earlier definition.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta

import asyncpg
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

# What get_bulk_user_turn_averages ran before, verbatim.
_TURNS_ORACLE_SQL = """
                SELECT user_id,
                       AVG(num_turns) AS avg_turns,
                       AVG(num_user_turns) AS avg_user_turns
                FROM api_logs
                WHERE user_id = ANY($1)
                GROUP BY user_id
                """

# What get_bulk_user_ask_question_fractions ran before, verbatim; $2 the tool
# names, $3 the turn cap.
_ASK_ORACLE_SQL = """
                SELECT user_id,
                       COUNT(*) AS n_requests,
                       COUNT(*) FILTER (
                           WHERE jsonb_typeof(tools) = 'array' AND EXISTS (
                               SELECT 1 FROM jsonb_array_elements(tools) AS t
                               WHERE lower(
                                       COALESCE(t->'function'->>'name', t->>'name')
                                     ) = ANY($2)
                           )
                       ) AS n_ask_requests
                FROM api_logs
                WHERE user_id = ANY($1)
                  AND num_turns <= $3
                GROUP BY user_id
                """

_USERS = ["heavy", "light", "quiet", "erased"]

_OPENAI_ASK = json.dumps([{"type": "function", "function": {"name": "AskUserQuestion"}}])
_ANTHROPIC_ASK = json.dumps([{"name": "read_file"}, {"name": "Ask_Followup_Question"}])
_NO_ASK = json.dumps([{"name": "read_file"}, {"type": "function", "function": {"name": "edit"}}])
_NOT_A_LIST = json.dumps({"name": "question"})
_BARE_NAMES = json.dumps(["question"])
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


async def _log(
    conn, user_id, at, tokens, *, status=200, error=None, turns=None, user_turns=None, tools=None
) -> None:
    """Insert one row; *at* is a naive UTC timestamp, or None for no timestamp."""
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, user_id, timestamp, "
        "total_tokens, cost_usd, status_code, error, num_turns, num_user_turns, tools) "
        "VALUES ($1, 'm', 'p', $2, $3::timestamp AT TIME ZONE 'UTC', $4, 0, $5, $6, "
        "$7, $8, $9::jsonb)",
        uuid.uuid4().hex,
        user_id,
        at,
        tokens,
        status,
        error,
        turns,
        user_turns,
        tools,
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
        cap = user_usage_rollup.ASK_QUESTION_TURN_CAP
        # Chat rows: (user, when, turns, user turns, tools).
        chat = [
            ("heavy", s - timedelta(days=40), 1, 1, _OPENAI_ASK),
            ("heavy", s - timedelta(days=3) - us, cap, 2, _ANTHROPIC_ASK),  # at the cap
            ("heavy", s - timedelta(days=3), cap + 1, 3, _OPENAI_ASK),  # past the cap
            ("heavy", s - timedelta(days=1), 2, None, _NO_ASK),  # no user-turn count
            ("heavy", s + timedelta(minutes=2), 3, 1, _NOT_A_LIST),
            ("heavy", None, 4, 2, _BARE_NAMES),  # no timestamp
            ("heavy", s - timedelta(days=2), None, None, _OPENAI_ASK),  # not a chat request
            ("light", s - timedelta(days=12), 7, 3, None),
            ("light", s - timedelta(days=2), 1, 1, None),
            ("erased", s - timedelta(days=5), 2, 1, _OPENAI_ASK),
            (None, s - timedelta(days=3), 1, 1, _OPENAI_ASK),  # no user: never counted
        ]
        for user_id, at, turns, user_turns, tools in chat:
            await _log(conn, user_id, at, 1, turns=turns, user_turns=user_turns, tools=tools)


async def _assert_behaviour_matches_oracle(pool) -> None:
    """Turn averages and the ask-question share, bulk and per user."""
    log_store = PostgresLogStore(pool, fence_secret=_FENCE_SECRET)
    async with pool.acquire() as conn:
        turns = {
            r["user_id"]: {
                "avg_turns": None if r["avg_turns"] is None else float(r["avg_turns"]),
                "avg_user_turns": (
                    None if r["avg_user_turns"] is None else float(r["avg_user_turns"])
                ),
            }
            for r in await conn.fetch(_TURNS_ORACLE_SQL, _USERS)
        }
        ask = {}
        for r in await conn.fetch(
            _ASK_ORACLE_SQL,
            _USERS,
            list(user_usage_rollup.ASK_QUESTION_TOOL_NAMES),
            user_usage_rollup.ASK_QUESTION_TURN_CAP,
        ):
            n, n_ask = int(r["n_requests"]), int(r["n_ask_requests"])
            ask[r["user_id"]] = {
                "ask_question_fraction": (n_ask / n) if n else None,
                "n_requests": n,
                "n_ask_requests": n_ask,
            }
    for got, want in (
        (await log_store.get_bulk_user_turn_averages(_USERS), turns),
        (await log_store.get_bulk_user_ask_question_fractions(_USERS), ask),
    ):
        assert got.keys() == want.keys()
        for uid in want:
            assert got[uid] == pytest.approx(want[uid])
    for uid in _USERS:
        detail = await log_store.get_user_detail_usage(uid, include_activity_stats=True)
        expected = turns.get(uid, {"avg_turns": None, "avg_user_turns": None})
        assert detail["avg_turns"] == pytest.approx(expected["avg_turns"])
        assert detail["avg_user_turns"] == pytest.approx(expected["avg_user_turns"])
        assert detail["ask_question_fraction"] == pytest.approx(
            ask.get(uid, {}).get("ask_question_fraction")
        )


async def _assert_matches_oracle(pool, op_store) -> None:
    async with pool.acquire() as conn:
        oracle = {uid: {"requests": 0, "tokens": 0} for uid in _USERS}
        for row in await conn.fetch(_ORACLE_SQL, _USERS):
            oracle[row["user_id"]] = {"requests": row["requests"], "tokens": row["tokens"]}
    assert await op_store.get_bulk_user_usage_totals(_USERS) == oracle
    await _assert_behaviour_matches_oracle(pool)

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
            (r["user_id"], r["day"]): tuple(r[c] for c, _ in user_usage_rollup.METRICS)
            for r in await conn.fetch("SELECT * FROM user_usage_daily")
        }
    # Boundary rows land on their own UTC day; a row without tokens adds none.
    # Columns: requests, tokens, turns sum/count, user turns sum/count, early
    # requests, early requests offering an ask tool.
    assert stored[("heavy", settled - timedelta(days=4))] == (2, 8, 5, 1, 2, 1, 1, 1)
    # Past the turn cap: counted in the averages, not in the ask share.
    assert stored[("heavy", settled - timedelta(days=3))] == (2, 12, 6, 1, 3, 1, 0, 0)
    # A chat row without a user-turn count leaves that average alone.
    assert stored[("heavy", settled - timedelta(days=1))] == (3, 6, 2, 1, 0, 0, 1, 0)
    assert all(user_id is not None for user_id, _ in stored)


async def test_dropped_days_are_counted_live_until_they_are_filled_again(db):
    pool, op_store, _, settled = db
    await _seed(pool, settled)
    await user_usage_rollup.backfill_missing(pool)

    # Two adjacent days, read as one run, and one apart from them.
    dropped = [settled - timedelta(days=d) for d in (3, 4, 12)]
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_usage_daily_coverage WHERE day = ANY($1::date[])", dropped
        )
        # A row without coverage is ignored, not added on top of the live count.
        await conn.execute(
            "UPDATE user_usage_daily SET request_count = 999 WHERE day = ANY($1::date[])",
            dropped,
        )
    await _assert_matches_oracle(pool, op_store)

    assert await user_usage_rollup.backfill_missing(pool) == 3
    await _assert_matches_oracle(pool, op_store)


async def test_days_counted_under_another_definition_are_counted_again(db):
    """A change to what a day stores retires the days stored before it."""
    pool, op_store, _, settled = db
    await _seed(pool, settled)
    await user_usage_rollup.backfill_missing(pool)

    stale = [settled - timedelta(days=3), settled - timedelta(days=40)]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_usage_daily_coverage SET definition = 'retired' "
            "WHERE day = ANY($1::date[])",
            stale,
        )
        # Wrong under the current definition; must be neither read nor added.
        await conn.execute(
            "UPDATE user_usage_daily SET turns_sum = 999, early_ask_requests = 999 "
            "WHERE day = ANY($1::date[])",
            stale,
        )
    await _assert_matches_oracle(pool, op_store)

    assert await user_usage_rollup.backfill_missing(pool) == 2
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM user_usage_daily_coverage WHERE definition <> $1",
                user_usage_rollup.DEFINITION,
            )
            == 0
        )
    await _assert_matches_oracle(pool, op_store)


async def test_upgrading_tables_from_before_the_turn_and_ask_columns(db):
    """Tables the first release created gain the columns, and every day is recounted."""
    pool, op_store, _, settled = db
    await _seed(pool, settled)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE user_usage_daily, user_usage_daily_coverage")
        # The first release's DDL, filled the way it filled them.
        await conn.execute("""
            CREATE TABLE user_usage_daily (
                user_id TEXT NOT NULL, day DATE NOT NULL,
                request_count BIGINT NOT NULL, total_tokens BIGINT NOT NULL,
                PRIMARY KEY (user_id, day)
            )
        """)
        await conn.execute("""
            CREATE TABLE user_usage_daily_coverage (
                day DATE PRIMARY KEY, completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            INSERT INTO user_usage_daily
            SELECT user_id, (timestamp AT TIME ZONE 'UTC')::date, COUNT(*),
                   COALESCE(SUM(total_tokens), 0)
            FROM api_logs WHERE user_id IS NOT NULL AND timestamp IS NOT NULL
            GROUP BY 1, 2
        """)
        await conn.execute(
            "INSERT INTO user_usage_daily_coverage (day) "
            "SELECT generate_series($1::date - 40, $1::date - 1, interval '1 day')::date",
            settled,
        )

        await ensure_api_logs_schema(conn)
        columns = {
            r["attname"]
            for r in await conn.fetch(
                "SELECT attname FROM pg_attribute "
                "WHERE attrelid = 'user_usage_daily'::regclass AND attnum > 0"
            )
        }
        assert {c for c, _ in user_usage_rollup.METRICS} <= columns
        # Days the first release stored kept their rows but not a definition.
        assert await conn.fetchval("SELECT count(*) FROM user_usage_daily_coverage") == 40

    # So none of them reads as stored: they are all counted live, then filled.
    await _assert_matches_oracle(pool, op_store)
    assert await user_usage_rollup.backfill_missing(pool) == 40
    await _assert_matches_oracle(pool, op_store)


async def _seed_stored_error(pool, settled) -> date:
    """A failed request on the last stored day, which a 48-hour clear reaches."""
    at = _midnight(settled) - timedelta(hours=1)
    async with pool.acquire() as conn:
        await _log(conn, "light", at, 123, status=500, error="boom")
    return at.date()


async def test_clear_errors_drops_exactly_the_days_it_deleted_from(db):
    pool, op_store, log_store, settled = db
    await _seed(pool, settled)
    error_day = await _seed_stored_error(pool, settled)
    await user_usage_rollup.backfill_missing(pool)
    stored = set(await _covered(pool))
    assert error_day in stored

    assert await log_store.delete_recent_error_requests(hours=48) == 1
    assert set(await _covered(pool)) == stored - {error_day}
    await _assert_matches_oracle(pool, op_store)

    assert await user_usage_rollup.backfill_missing(pool) == 1
    await _assert_matches_oracle(pool, op_store)


async def test_a_failed_invalidation_rolls_the_clear_back(db, monkeypatch):
    """The delete and the invalidation commit together or not at all.

    Were they separate, the delete could commit and the invalidation fail: the
    day would stay marked counted with the deleted request in it, no backfill
    would count it again, and a second clear would delete nothing and so
    invalidate nothing.
    """
    pool, op_store, log_store, settled = db
    await _seed(pool, settled)
    error_day = await _seed_stored_error(pool, settled)
    await user_usage_rollup.backfill_missing(pool)

    async def fail(conn, days):
        raise asyncpg.QueryCanceledError("canceling statement due to statement timeout")

    monkeypatch.setattr(user_usage_rollup, "invalidate_days", fail)
    with pytest.raises(asyncpg.QueryCanceledError):
        await log_store.delete_recent_error_requests(hours=48)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM api_logs WHERE error = 'boom'") == 1
    assert error_day in await _covered(pool)
    await _assert_matches_oracle(pool, op_store)

    # The admin retries once the cause is gone, and the clear goes through.
    monkeypatch.undo()
    assert await log_store.delete_recent_error_requests(hours=48) == 1
    assert error_day not in await _covered(pool)
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

    assert counts["api_logs"] == 3
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
