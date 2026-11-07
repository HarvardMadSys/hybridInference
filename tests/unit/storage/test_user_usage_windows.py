"""/user/usage reads only the windows it shows, through the rollup, cached per user.

It used to aggregate every row the user ever logged on every call -- the
all-time window was always computed, even for ``period=today``. The figures
equal the full scan (``tests/integration/storage/test_user_usage_windows_postgres.py``
holds the SQL to it against PostgreSQL); these pin what is asked for, what the
statement reads, and the cache in front of it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers import user_routes
from serving.storage import postgres_log, user_usage_rollup
from serving.storage.postgres_log import PostgresLogStore

pytestmark = pytest.mark.asyncio

_WINDOW = {
    "requests": 1,
    "cost_usd": 0.5,
    "prompt_tokens": 1,
    "completion_tokens": 1,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": None,
    "reasoning_tokens_known_requests": 0,
}


# ---------------------------------------------------------------------------
# The route asks for the period shown and the month, nothing else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "window"),
    [("today", "today"), ("week", "week"), ("month", "month"), ("all", "alltime")],
)
async def test_the_route_asks_for_its_period_and_the_month(monkeypatch, period, window):
    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = {"quota_daily_cost_usd": 5.0}
    op_store.get_user_cost_today.return_value = 0.0
    log_store = AsyncMock()
    log_store.get_user_usage_detail.return_value = {
        "month": {**_WINDOW, "cost_usd": 2.25},
        window: {**_WINDOW, "requests": 7, "cost_usd": 2.25},
    }

    async def _cap(role, rt, *, is_admin=False):
        return 1

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _cap)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)

    resp = await user_routes.get_usage(
        period=period,
        current_user={"user_id": "u1", "role": "free"},
        op_store=op_store,
        log_store=log_store,
    )

    log_store.get_user_usage_detail.assert_awaited_once_with("u1", windows=(window, "month"))
    assert resp.usage.requests == 7
    assert resp.quota.spent_month_usd == 2.25


# ---------------------------------------------------------------------------
# The statement reads only what the windows need
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "windows", [{"today"}, {"today", "month"}, {"week", "month"}, {"month"}, {"week"}]
)
def test_without_alltime_nothing_before_the_earliest_window_is_read(windows):
    sql = user_usage_rollup.usage_windows_sql(windows)

    # No unbounded part: no rows without a timestamp, no history from the first
    # logged day, and the stored days start at a window's first day.
    assert "timestamp IS NULL" not in sql
    assert "MIN(timestamp)" not in sql
    assert " AND d.day >= " in sql
    # Every window asked for is summed, and no other.
    for window in user_usage_rollup.USAGE_WINDOWS:
        assert (f"AS {window}__request_count" in sql) is (window in windows)


def test_alltime_reads_the_whole_history_once():
    sql = user_usage_rollup.usage_windows_sql({"alltime", "month"})

    assert sql.count("timestamp IS NULL") == 1
    assert "MIN(timestamp) AT TIME ZONE 'UTC')::date FROM api_logs WHERE user_id = $1" in sql
    assert " AND d.day >= " not in sql


def test_the_week_counts_its_partial_first_day_live():
    assert "TRUE AS head" in user_usage_rollup.usage_windows_sql({"week"})
    assert "TRUE AS head" not in user_usage_rollup.usage_windows_sql({"today", "month"})


@pytest.mark.parametrize("windows", [set(), {"yesterday"}, {"today", "decade"}])
def test_unknown_or_no_windows_are_refused(windows):
    with pytest.raises(ValueError):
        user_usage_rollup.usage_windows_sql(windows)


def test_the_sums_have_a_fingerprint_of_their_own():
    """Adding them must not retire the days stored for the admin columns."""
    assert user_usage_rollup.USAGE_DEFINITION != user_usage_rollup.DEFINITION
    stored = {column for column, _ in user_usage_rollup.METRICS}
    assert stored.isdisjoint(column for column, _ in user_usage_rollup.USAGE_METRICS)


# ---------------------------------------------------------------------------
# The store's cache
# ---------------------------------------------------------------------------


def _pool() -> MagicMock:
    conn = MagicMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


@pytest.fixture
def reads(monkeypatch) -> list[tuple[str, tuple[str, ...]]]:
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def fake_fetch(conn, user_id, windows):
        calls.append((user_id, tuple(windows)))
        await asyncio.sleep(0)
        return {window: {**_WINDOW, "requests": len(calls)} for window in windows}

    monkeypatch.setattr(user_usage_rollup, "fetch_usage_windows", fake_fetch)
    return calls


async def test_a_user_is_read_once_within_the_ttl(reads):
    store = PostgresLogStore(_pool())

    first = await store.get_user_usage_detail("u1", windows=("today", "month"))
    second = await store.get_user_usage_detail("u1", windows=("month", "today"))

    assert first == second
    assert reads == [("u1", ("today", "month"))]


async def test_concurrent_calls_share_one_read(reads):
    store = PostgresLogStore(_pool())

    results = await asyncio.gather(
        *(store.get_user_usage_detail("u1", windows=("today", "month")) for _ in range(25))
    )

    assert len(reads) == 1
    assert all(result == results[0] for result in results)


async def test_users_and_window_sets_are_read_separately(reads):
    store = PostgresLogStore(_pool())

    await store.get_user_usage_detail("u1", windows=("today", "month"))
    await store.get_user_usage_detail("u2", windows=("today", "month"))
    await store.get_user_usage_detail("u1", windows=("alltime", "month"))
    detail = await store.get_user_usage_detail("u1")

    assert reads == [
        ("u1", ("today", "month")),
        ("u2", ("today", "month")),
        ("u1", ("month", "alltime")),
        ("u1", user_usage_rollup.USAGE_WINDOWS),
    ]
    assert detail.keys() == set(user_usage_rollup.USAGE_WINDOWS)


async def test_a_caller_cannot_change_what_the_next_one_is_told(reads):
    store = PostgresLogStore(_pool())

    first = await store.get_user_usage_detail("u1", windows=("today",))
    first["today"]["requests"] = -1

    assert (await store.get_user_usage_detail("u1", windows=("today",)))["today"]["requests"] == 1


async def test_the_ttl_is_about_half_a_minute():
    assert 15 <= postgres_log.USER_USAGE_CACHE_TTL_SEC <= 60
