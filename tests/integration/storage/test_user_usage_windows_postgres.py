"""The dashboard's usage windows, read through the rollup, equal the full scan they replace.

``/user/usage`` used to sum every ``api_logs`` row a user ever logged on every
call (the all-time window), whatever period was asked for. It now reads the
completed days the daily rollup stored plus the rest live, and only the windows
asked for (``serving.storage.user_usage_rollup.fetch_usage_windows``). These run
the real SQL and hold it to the query it replaced -- kept here verbatim as the
oracle, with its result shaping -- over rows placed on every seam: UTC day and
month boundaries, the settled day, the rolling week's start, rows without a
timestamp, a cost, a token or a reasoning count, and days the rollup has not
stored, has dropped, stored under another fingerprint, or stored before the
sums existed.

Each comparison runs the oracle and the new read in one transaction, so both
see the same ``NOW()`` and the same rows.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from itertools import combinations

import pytest
import pytest_asyncio

from serving.storage import user_usage_rollup
from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import migrations as mig, postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest, pytest.mark.asyncio]

# ---------------------------------------------------------------------------
# The oracle: what get_user_usage_detail ran before, verbatim.
# ---------------------------------------------------------------------------

_USER_USAGE_WINDOWS = {
    "today": "timestamp >= (date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')",
    "week": "timestamp >= NOW() - INTERVAL '7 days'",
    "month": "timestamp >= (date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')",
    "alltime": "TRUE",
}

_USER_USAGE_SUMS = (
    "cost_usd",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)

_USER_USAGE_KNOWN_SUMS = ("reasoning_tokens",)

_USER_USAGE_SQL = "SELECT {} FROM api_logs WHERE user_id = $1".format(
    ",\n       ".join(
        [
            f"COUNT(*) FILTER (WHERE {cond}) AS {window}__requests"
            for window, cond in _USER_USAGE_WINDOWS.items()
        ]
        + [
            (
                f"SUM({col}) FILTER (WHERE {cond}) AS {window}__{col}, "
                f"COUNT({col}) FILTER (WHERE {cond}) AS {window}__{col}_known"
            )
            if col in _USER_USAGE_KNOWN_SUMS
            else f"COALESCE(SUM({col}) FILTER (WHERE {cond}), 0) AS {window}__{col}"
            for window, cond in _USER_USAGE_WINDOWS.items()
            for col in _USER_USAGE_SUMS
        ]
    )
)


def _oracle_window(row, window: str) -> dict:
    stats = {
        "requests": int(row[f"{window}__requests"]) if row else 0,
    }
    for col in _USER_USAGE_SUMS:
        if col in _USER_USAGE_KNOWN_SUMS:
            known = int(row[f"{window}__{col}_known"] or 0) if row else 0
            value = row[f"{window}__{col}"] if row and known else None
            stats[col] = None if value is None else int(value)
            stats[f"{col}_known_requests"] = known
            continue
        value = row[f"{window}__{col}"] if row else 0
        stats[col] = float(value) if col == "cost_usd" else int(value)
    return stats


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_USERS = ["heavy", "light", "undated", "nothing-known", "absent"]
_WINDOW_SETS = [
    set(c)
    for n in range(1, len(user_usage_rollup.USAGE_WINDOWS) + 1)
    for c in combinations(user_usage_rollup.USAGE_WINDOWS, n)
]


@pytest_asyncio.fixture
async def db():
    """An isolated schema with the log and rollup tables, as the migrations leave them."""
    schema = "usage_windows_" + uuid.uuid4().hex
    admin = await postgres.connect()
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await postgres.create_pool(
        min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            for table in ("users", "api_keys", "user_daily_cost"):
                await conn.execute(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
            await mig.ensure_api_logs_tables(conn)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _log(conn, user_id, at, *, cost, prompt, completion, cache_read, cache_write, reasoning):
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, user_id, timestamp, cost_usd, "
        "prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens, "
        "reasoning_tokens, total_tokens, status_code) "
        "VALUES ($1, 'm', 'p', $2, $3, $4, $5, $6, $7, $8, $9, 0, 200)",
        uuid.uuid4().hex,
        user_id,
        at,
        cost,
        prompt,
        completion,
        cache_read,
        cache_write,
        reasoning,
    )


async def _seed(pool) -> dict:
    """Rows on every seam, placed relative to the database's own clock."""
    async with pool.acquire() as conn:
        clock = await conn.fetchrow(
            "SELECT NOW() AS now, "
            "date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS today, "
            "date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS month, "
            f"({user_usage_rollup.SETTLED_DAY_SQL}::timestamp AT TIME ZONE 'UTC') AS settled"
        )
        now, today, month, settled = clock["now"], clock["today"], clock["month"], clock["settled"]
        us = timedelta(microseconds=1)
        week = now - timedelta(days=7)
        seams = [
            today - timedelta(days=400),  # more than a year back
            month - timedelta(days=31) + timedelta(hours=5),  # last month, mid-day
            month - us,  # the month's last instant before this one
            month,  # this month's first instant
            month + timedelta(hours=1),
            week - timedelta(minutes=1),  # just before the rolling week
            week + timedelta(seconds=30),  # inside it, on its partial first day
            week + timedelta(days=1),
            settled - timedelta(days=2) - us,  # a day's last instant
            settled - timedelta(days=2),  # the next day's first
            settled - us,  # the last stored instant
            settled,  # the first live one
            today - us,
            today,
            now - timedelta(seconds=1),
        ]
        # A cost with every decimal place, a token count of each kind, and the
        # values a row can lack.
        fields = ("cost", "prompt", "completion", "cache_read", "cache_write", "reasoning")
        values = [
            dict(zip(fields, row, strict=True))
            for row in (
                (Decimal("0.12345678"), 1000, 200, 300, 40, 17),
                (Decimal("1.5"), 7, None, None, 0, None),
                (None, None, 3, 1, None, 0),
                (Decimal("2500.00000001"), 2_000_000_000, 2_000_000_000, 0, 1, 2_000_000_000),
            )
        ]
        for i, at in enumerate(seams):
            for j in range(1 + i % 3):
                await _log(conn, "heavy", at, **values[(i + j) % len(values)])
        await _log(conn, "heavy", None, **values[0])  # no timestamp
        await _log(conn, "light", month + timedelta(days=1), **values[1])
        await _log(conn, "light", today - timedelta(days=90), **values[2])
        await _log(conn, "undated", None, **values[3])
        for at in (today - timedelta(days=40), now - timedelta(minutes=5)):
            await _log(
                conn,
                "nothing-known",
                at,
                cost=None,
                prompt=None,
                completion=None,
                cache_read=None,
                cache_write=None,
                reasoning=None,
            )
        # A row with no user is in no one's figures.
        await _log(conn, None, today, **values[0])
    return {"now": now, "today": today, "settled": settled}


async def _compare(pool, windows: set[str]) -> None:
    """Oracle and rollup read agree for every user, in one snapshot."""
    async with pool.acquire() as conn:
        for user_id in _USERS:
            async with conn.transaction(isolation="repeatable_read"):
                oracle_row = await conn.fetchrow(_USER_USAGE_SQL, user_id)
                got = await user_usage_rollup.fetch_usage_windows(conn, user_id, windows)
            want = {w: _oracle_window(oracle_row, w) for w in windows}
            assert got == want, (user_id, sorted(windows))
            # Same types too: the dashboard renders them.
            for window in windows:
                assert type(got[window]["cost_usd"]) is float
                assert all(
                    type(got[window][c]) is int
                    for c in ("requests", "prompt_tokens", "completion_tokens")
                )


async def _compare_every_window_set(pool) -> None:
    for windows in _WINDOW_SETS:
        await _compare(pool, windows)


async def _stored_days(pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM user_usage_daily_coverage "
            "WHERE definition = $1 AND usage_definition = $2",
            user_usage_rollup.DEFINITION,
            user_usage_rollup.USAGE_DEFINITION,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_every_window_matches_with_nothing_stored(db):
    await _seed(db)
    assert await _stored_days(db) == 0

    await _compare_every_window_set(db)


async def test_every_window_matches_once_the_days_are_stored(db):
    await _seed(db)
    assert await user_usage_rollup.backfill_missing(db) > 300
    assert await _stored_days(db) > 300

    await _compare_every_window_set(db)


async def test_dropped_days_are_counted_live_not_added(db):
    clock = await _seed(db)
    await user_usage_rollup.backfill_missing(db)
    settled_day = clock["settled"].date()
    # Adjacent days read as one run, and one apart; one is the week's first.
    week_first = (clock["now"] - timedelta(days=7)).date()
    dropped = [settled_day - timedelta(days=d) for d in (1, 2)] + [week_first]
    async with db.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_usage_daily_coverage WHERE day = ANY($1::date[])", dropped
        )
        # A stored row without coverage must be neither read nor added.
        await conn.execute(
            "UPDATE user_usage_daily SET request_count = 999, cost_usd = 999, "
            "prompt_tokens = 999, reasoning_tokens_known = 999 WHERE day = ANY($1::date[])",
            dropped,
        )

    await _compare_every_window_set(db)


async def test_days_stored_before_the_sums_existed_are_counted_live(db):
    """Days the previous release filled carry no usage fingerprint and zero sums."""
    await _seed(db)
    await user_usage_rollup.backfill_missing(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE user_usage_daily_coverage SET usage_definition = '' "
            "WHERE day IN (SELECT day FROM user_usage_daily_coverage ORDER BY day DESC LIMIT 20)"
        )
        await conn.execute(
            "UPDATE user_usage_daily SET cost_usd = 0, prompt_tokens = 0, completion_tokens = 0, "
            "cache_read_tokens = 0, cache_write_tokens = 0, reasoning_tokens = 0, "
            "reasoning_tokens_known = 0 "
            "WHERE day IN (SELECT day FROM user_usage_daily_coverage WHERE usage_definition = '')"
        )

    await _compare_every_window_set(db)
    # And the next backfill stores them again, under both fingerprints.
    assert await user_usage_rollup.backfill_missing(db) == 20
    await _compare_every_window_set(db)


async def test_days_under_another_admin_definition_are_counted_live(db):
    await _seed(db)
    await user_usage_rollup.backfill_missing(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE user_usage_daily_coverage SET definition = 'retired' "
            "WHERE day IN (SELECT day FROM user_usage_daily_coverage ORDER BY day LIMIT 50)"
        )
        await conn.execute(
            "UPDATE user_usage_daily SET cost_usd = cost_usd + 1 "
            "WHERE day IN (SELECT day FROM user_usage_daily_coverage "
            "WHERE definition = 'retired')"
        )

    await _compare_every_window_set(db)


async def test_the_store_reads_the_same_and_only_what_was_asked(db):
    await _seed(db)
    await user_usage_rollup.backfill_missing(db)

    for windows in (None, {"today", "month"}, {"alltime", "month"}, {"week"}):
        for user_id in _USERS:
            # A fresh store each time: this is about the read, not the cache.
            store = PostgresLogStore(db, fence_secret=postgres.FENCE_SECRET)
            async with db.acquire() as conn, conn.transaction(isolation="repeatable_read"):
                oracle_row = await conn.fetchrow(_USER_USAGE_SQL, user_id)
                asked = windows or set(user_usage_rollup.USAGE_WINDOWS)
                want = {w: _oracle_window(oracle_row, w) for w in asked}
            got = await store.get_user_usage_detail(user_id, windows=windows)
            assert got.keys() == want.keys()
            # The week moves with the clock; the rest is exact between the two reads.
            for window in asked - {"week"}:
                assert got[window] == want[window], (user_id, window)


async def test_unknown_windows_are_refused(db):
    with pytest.raises(ValueError):
        user_usage_rollup.usage_windows_sql({"yesterday"})
    with pytest.raises(ValueError):
        user_usage_rollup.usage_windows_sql(set())
    store = PostgresLogStore(db, fence_secret=postgres.FENCE_SECRET)
    with pytest.raises(ValueError):
        await store.get_user_usage_detail("heavy", windows={"today", "decade"})


async def test_results_are_cached_per_user_and_window_set(db):
    clock = await _seed(db)
    store = PostgresLogStore(db, fence_secret=postgres.FENCE_SECRET)
    before = await store.get_user_usage_detail("light", windows={"today", "month"})

    async with db.acquire() as conn:
        await _log(
            conn,
            "light",
            clock["now"],
            cost=Decimal("5"),
            prompt=1,
            completion=1,
            cache_read=0,
            cache_write=0,
            reasoning=None,
        )
    # Within the TTL, the same answer...
    assert await store.get_user_usage_detail("light", windows={"month", "today"}) == before
    # ... another window set is read afresh ...
    fresh = await store.get_user_usage_detail("light", windows={"today"})
    assert fresh["today"]["requests"] == before["today"]["requests"] + 1
    # ... and a caller cannot change what the next one is told.
    before["today"]["requests"] = -1
    again = await store.get_user_usage_detail("light", windows={"today", "month"})
    assert again["today"]["requests"] != -1
    # The cache is the store's own: a new store reads the new row.
    other = PostgresLogStore(db, fence_secret=postgres.FENCE_SECRET)
    assert (await other.get_user_usage_detail("light", windows={"month"}))["month"][
        "cost_usd"
    ] == pytest.approx(before["month"]["cost_usd"] + 5)
