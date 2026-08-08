"""The growth series against a real PostgreSQL instance.

Covers what a mocked connection cannot: that the day buckets are whole UTC days
whatever timezone the database session happens to be in, including across a DST
transition.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

from serving.servers.routers.admin.analytics import _GROWTH_SERIES_SQL

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn


@pytest_asyncio.fixture
async def conn(pg_dsn: str):
    connection = await asyncpg.connect(pg_dsn)
    await connection.execute("""
        CREATE TABLE IF NOT EXISTS api_logs (
            id BIGSERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT NOW(),
            request_id TEXT NOT NULL UNIQUE,
            model_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            user_id TEXT
        )
    """)
    await connection.execute("TRUNCATE TABLE api_logs")
    try:
        yield connection
    finally:
        await connection.close()


async def _insert(connection, ts: datetime, request_id: str, user_id: str | None) -> None:
    await connection.execute(
        "INSERT INTO api_logs (timestamp, request_id, model_id, provider,"
        " prompt_tokens, completion_tokens, user_id) VALUES ($1,$2,'m','p',100,10,$3)",
        ts,
        request_id,
        user_id,
    )


async def _fetch(connection, start_day: datetime, end_day: datetime):
    return await connection.fetch(
        _GROWTH_SERIES_SQL, start_day, end_day, end_day + timedelta(days=1)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("session_tz", ["UTC", "America/New_York", "Australia/Sydney"])
async def test_buckets_stay_on_utc_midnight_across_dst(conn, session_tz: str) -> None:
    """Buckets must not drift when the range crosses a daylight-saving change.

    2026-03-08 is the US spring-forward. Stepping a ``timestamptz`` by
    ``interval '1 day'`` is a *calendar* day in the session timezone, so from
    that date on every generated bucket lands at 23:00 UTC instead of midnight,
    matches nothing in the join, and reports real traffic as zero.
    """
    await conn.execute(f"SET TIME ZONE '{session_tz}'")

    start_day = datetime(2026, 3, 1, tzinfo=timezone.utc)
    end_day = datetime(2026, 3, 14, tzinfo=timezone.utc)
    days = 14

    # One request per day, at noon UTC so no row sits near a boundary.
    for i in range(days):
        await _insert(conn, start_day + timedelta(days=i, hours=12), f"r{i}", f"u{i}")

    rows = await _fetch(conn, start_day, end_day)

    assert len(rows) == days
    assert all(r["day"].astimezone(timezone.utc).hour == 0 for r in rows), (
        f"buckets drifted off UTC midnight under {session_tz}: "
        f"{[r['day'].astimezone(timezone.utc).isoformat() for r in rows]}"
    )
    # Every day carries its one request; a drifted bucket would report zero.
    assert [r["active_users"] for r in rows] == [1] * days
    assert [r["requests"] for r in rows] == [1] * days


@pytest.mark.asyncio
async def test_day_attribution_and_gap_fill(conn) -> None:
    """A non-UTC session must not pull rows into the neighbouring UTC day."""
    await conn.execute("SET TIME ZONE 'America/New_York'")

    start_day = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end_day = datetime(2026, 6, 5, tzinfo=timezone.utc)

    # 23:30 UTC on the 1st is still the 1st in UTC but already the 2nd in
    # New York, so a session-timezone bucket would misfile it.
    await _insert(conn, datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc), "late", "alice")
    await _insert(conn, datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc), "early", "bob")
    # Returning user: active again on the 3rd, so not new that day.
    await _insert(conn, datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc), "again", "alice")
    # Anonymous traffic counts toward tokens but never toward DAU.
    await _insert(conn, datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc), "anon", None)
    # Outside the window entirely.
    await _insert(conn, datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc), "before", "carol")

    rows = await _fetch(conn, start_day, end_day)
    by_day = {r["day"].astimezone(timezone.utc).date(): r for r in rows}

    assert len(rows) == 5

    first = by_day[start_day.date()]
    assert first["active_users"] == 2
    assert first["new_users"] == 2
    assert first["requests"] == 2

    third = by_day[datetime(2026, 6, 3).date()]
    assert third["active_users"] == 1  # alice only; the anonymous row is excluded
    assert third["new_users"] == 0  # alice was already seen on the 1st
    assert third["requests"] == 2  # but both rows count as requests
    assert third["tokens"] == 220

    empty = by_day[datetime(2026, 6, 2).date()]
    assert empty["active_users"] == 0
    assert empty["tokens"] == 0

    # carol is outside the window and must not appear anywhere.
    assert sum(r["new_users"] for r in rows) == 2
