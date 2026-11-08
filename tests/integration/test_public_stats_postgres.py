"""The public-stats job against PostgreSQL, across runs.

The country rollup keeps 90 days, less than the 26-week window, so a week the
rollup has dropped takes its weekly country counts from the previous snapshot
instead of falling to zero, and the totals say from which day they count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio

from serving.analytics import public_stats as ps
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

# Mondays, oldest first; NOW is a Wednesday afternoon in the third week.
W1, W2, W3 = (datetime(2026, 7, 27, tzinfo=UTC) + timedelta(weeks=i) for i in range(3))
NOW = datetime(2026, 8, 12, 14, 27, tzinfo=UTC)

_TABLES = (
    "geo_hourly_demand",
    "geo_hourly_coverage",
    "public_stats_snapshots",
    "api_log_retained_daily",
    "api_log_retained_language",
    "api_log_retained_language_sample",
    "api_log_retained_client",
    "api_logs",
)


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    return postgres.worker_dsn()


@pytest_asyncio.fixture
async def conn(pg_dsn: str):
    # A plain connection, as the job opens one: jsonb comes back as text.
    connection = await asyncpg.connect(pg_dsn)
    await connection.execute(f"TRUNCATE TABLE {', '.join(_TABLES)}")
    try:
        yield connection
    finally:
        await connection.execute(f"TRUNCATE TABLE {', '.join(_TABLES)}")
        await connection.close()


async def _country(conn, hour: datetime, country: str, continent: str, requests: int) -> None:
    await conn.execute(
        "INSERT INTO geo_hourly_coverage (hour_bucket, rows_total, rows_with_ip) "
        "VALUES ($1, $2, $2) ON CONFLICT (hour_bucket) DO NOTHING",
        hour,
        requests,
    )
    await conn.execute(
        "INSERT INTO geo_hourly_demand "
        "(hour_bucket, country_code, continent_code, request_count, completion_tokens) "
        "VALUES ($1, $2, $3, $4, 0)",
        hour,
        country,
        continent,
        requests,
    )


async def _request(conn, at: datetime) -> None:
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, timestamp, status_code) "
        "VALUES ($1, 'm', 'p', $2, 200)",
        f"req-{at.isoformat()}",
        at,
    )


async def _run(conn) -> dict:
    retention: list[ps.RetentionWrite] = []
    payload = await ps.compute_snapshot(conn, now=NOW, retention_out=retention)
    await ps.store_snapshot(conn, payload, retention[0])
    return payload


async def test_a_week_the_country_rollup_dropped_keeps_its_weekly_counts(conn):
    await _request(conn, W1 + timedelta(hours=1))
    for country, continent in (("FRA", "EU"), ("USA", "NA"), ("KEN", "AF")):
        await _country(conn, W1 + timedelta(days=1, hours=3), country, continent, 150)
    await _country(conn, W2 + timedelta(days=2, hours=5), "FRA", "EU", 20)

    first = await _run(conn)
    assert first["countries"]["weekly"][:2] == [
        {"any": 3, "min_requests": 3},
        {"any": 1, "min_requests": 0},
    ]
    assert first["countries"]["total"] == 3

    # The rollup's purge reaches W1 (its rows go with their coverage).
    await conn.execute("DELETE FROM geo_hourly_coverage WHERE hour_bucket < $1", W2)

    second = await _run(conn)
    countries = second["countries"]
    assert countries["weekly"] == [
        {"any": 3, "min_requests": 3},  # from the first snapshot
        {"any": 1, "min_requests": 0},
        {"any": 0, "min_requests": 0},
    ]
    # The totals count what the rollup still holds, and say from when.
    assert countries["total"] == 1
    assert countries["since"] == (W2 + timedelta(days=2, hours=5)).date().isoformat()


async def test_the_first_run_carries_nothing(conn):
    await _request(conn, W2)
    await _country(conn, W2 + timedelta(hours=7), "DEU", "EU", 5)

    payload = await _run(conn)

    # The window starts at the first log, in W2.
    assert payload["weeks"] == [W2.date().isoformat(), W3.date().isoformat()]
    assert payload["countries"]["weekly"] == [
        {"any": 1, "min_requests": 0},
        {"any": 0, "min_requests": 0},
    ]
    assert payload["countries"]["since"] == W2.date().isoformat()
