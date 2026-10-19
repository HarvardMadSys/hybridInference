"""Today, this month and an hour mean UTC, whatever the database session's zone.

``date_trunc('day', NOW() AT TIME ZONE 'UTC')`` yields a timestamp *without* a
zone, which PostgreSQL compares against ``api_logs.timestamp`` by reading it in
the session's time zone. Under a non-UTC session "today" started hours off:
measured before the fix, a user's spend today read 0.0 in America/Los_Angeles
and 11.0 in Asia/Shanghai for a true 1.0 -- the number the daily quota is
enforced on. ``date_trunc('hour', timestamp)`` likewise bucketed by the
session's clock, which a half-hour zone (Asia/Kolkata) never aligns with UTC.

Every query below runs under each zone and must give the UTC answer. Rows sit
thirty minutes either side of UTC midnight and of the month's start, so any
window the session's zone moves shows up as a different sum.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from serving.admin import provider_stats_rollup
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

ZONES = ["UTC", "America/Los_Angeles", "Asia/Shanghai", "Asia/Kolkata"]
USER = "u-utcwin-a"


def _now() -> datetime:
    return datetime.now(UTC)


def _midnight() -> datetime:
    return _now().replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start() -> datetime:
    return _midnight().replace(day=1)


#: (request id suffix, timestamp, cost): either side of each UTC boundary.
def _rows() -> list[tuple[str, datetime, Decimal]]:
    return [
        ("today", _midnight() + timedelta(minutes=30), Decimal("1")),
        ("yesterday", _midnight() - timedelta(minutes=30), Decimal("10")),
        ("last-month", _month_start() - timedelta(minutes=30), Decimal("100")),
    ]


def _expected(since: datetime) -> float:
    return float(sum(cost for _, at, cost in _rows() if at >= since))


@pytest_asyncio.fixture
async def seeded():
    pool = await postgres.create_pool(min_size=1, max_size=3)
    log_store = PostgresLogStore(pool, fence_secret=postgres.FENCE_SECRET)
    await log_store.initialize()
    op_store = PostgresOperationalStore(pool)
    await op_store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'req-utcwin-%'")
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1", USER)
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id = $1", USER)
            await conn.execute("DELETE FROM users WHERE id = $1", USER)
            await conn.execute("DELETE FROM provider_hourly_stats WHERE provider LIKE 'utcwin-%'")

    await _wipe()
    await op_store.create_user(user_id=USER, email=f"{USER}@utcwin.test", password_hash="x")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id, "
            "quota_daily_cost_usd) VALUES ('h-utcwin', 'sk-utcwin', $1, $1, 1.2)",
            USER,
        )
        for suffix, at, cost in _rows():
            # The daily counter the operational store reads, keyed by UTC day.
            await conn.execute(
                "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests) "
                "VALUES ($1, $2, $3, 1) ON CONFLICT (user_id, day) DO UPDATE "
                "SET cost_usd = user_daily_cost.cost_usd + EXCLUDED.cost_usd",
                USER,
                at.date().isoformat(),
                cost,
            )
            await conn.execute(
                "INSERT INTO api_logs (request_id, model_id, provider, timestamp, user_id, "
                "cost_usd, prompt_tokens, completion_tokens) "
                "VALUES ($1, 'm', 'utcwin-p', $2, $3, $4, 10, 5)",
                f"req-utcwin-{suffix}",
                at,
                USER,
                cost,
            )
    try:
        yield pool
    finally:
        await _wipe()
        await pool.close()


async def _in_zone(zone: str) -> Any:
    return await postgres.create_pool(min_size=1, max_size=2, server_settings={"timezone": zone})


async def _each_zone(call) -> dict[str, Any]:
    results = {}
    for zone in ZONES:
        pool = await _in_zone(zone)
        try:
            results[zone] = await call(pool)
        finally:
            await pool.close()
    return results


def _log(pool: Any) -> PostgresLogStore:
    return PostgresLogStore(pool, fence_secret=postgres.FENCE_SECRET)


async def test_spend_today_is_since_utc_midnight(seeded):
    """The daily quota is enforced on this number (verify_api_key, grants)."""
    results = await _each_zone(lambda pool: _log(pool).get_user_cost_today(USER))

    assert results == dict.fromkeys(ZONES, _expected(_midnight()))


@pytest.mark.parametrize(
    ("period", "since"), [("today", _midnight), ("month", _month_start)], ids=["today", "month"]
)
async def test_batch_spend_uses_utc_windows(seeded, period, since):
    log_results = await _each_zone(lambda pool: _log(pool).get_batch_usage([USER], period))
    op_results = await _each_zone(
        lambda pool: PostgresOperationalStore(pool).get_batch_usage([USER], period)
    )

    expected = {USER: _expected(since())}
    assert {
        zone: {k: float(v) for k, v in r.items()} for zone, r in log_results.items()
    } == dict.fromkeys(ZONES, expected)
    assert {
        zone: {k: float(v) for k, v in r.items()} for zone, r in op_results.items()
    } == dict.fromkeys(ZONES, expected)


@pytest.mark.parametrize(
    "read",
    [
        lambda pool: _log(pool).get_user_usage_detail(USER),
        lambda pool: _log(pool).get_key_detail_usage(USER),
        lambda pool: _log(pool).get_user_detail_usage(USER),
    ],
    ids=["user-usage-detail", "key-detail-usage", "admin-user-detail"],
)
async def test_usage_detail_reads_the_same_in_every_zone(seeded, read):
    results = await _each_zone(read)

    assert all(result == results["UTC"] for result in results.values()), results


async def test_usage_detail_today_and_month_are_utc(seeded):
    detail = (await _each_zone(lambda pool: _log(pool).get_user_usage_detail(USER)))[
        "Asia/Shanghai"
    ]

    assert float(detail["today"]["cost_usd"]) == _expected(_midnight())
    assert float(detail["month"]["cost_usd"]) == _expected(_month_start())


@pytest.mark.parametrize(
    ("filters", "listed"),
    [
        ({"min_cost_today": Decimal("1")}, True),
        ({"min_cost_today": Decimal("1.5")}, False),  # yesterday's 10 must not count
        ({"quota_state": "near"}, True),  # 1 of 1.2 >= 80%
        ({"quota_state": "over"}, False),
        ({"min_cost_month": Decimal(str(_expected(_month_start())))}, True),
        ({"min_cost_month": Decimal(str(_expected(_month_start()) + 0.5))}, False),
    ],
    ids=["today-1", "today-1.5", "near", "over", "month-exact", "month-above"],
)
async def test_admin_user_filters_use_utc_windows(seeded, filters, listed):
    results = await _each_zone(
        lambda pool: PostgresOperationalStore(pool).list_users(search="utcwin.test", **filters)
    )

    assert {zone: total for zone, (total, _rows, _counts) in results.items()} == dict.fromkeys(
        ZONES, 1 if listed else 0
    )


async def test_admin_user_usage_columns_use_utc_windows(seeded):
    results = await _each_zone(
        lambda pool: PostgresOperationalStore(pool).list_users(
            search="utcwin.test", sort_by="cost_today"
        )
    )

    for zone, (_total, rows, _counts) in results.items():
        assert (float(rows[0]["usage_today"]), float(rows[0]["usage_month"])) == (
            _expected(_midnight()),
            _expected(_month_start()),
        ), zone


async def test_provider_spend_for_a_utc_hour(seeded):
    hour = (_midnight() + timedelta(minutes=30)).replace(minute=0)

    results = await _each_zone(
        lambda pool: _log(pool).query_provider_hourly_spend(hour.isoformat())
    )

    assert results == {zone: {"utcwin-p": 1.0} for zone in ZONES}


async def test_hourly_provider_stats_bucket_by_utc_hour(seeded):
    hour = _midnight()

    async def _bucket(pool: Any) -> list[datetime]:
        await provider_stats_rollup.run_rollup(pool, start=hour, end=hour + timedelta(hours=1))
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT hour_bucket FROM provider_hourly_stats WHERE provider = 'utcwin-p'"
            )
            await conn.execute("DELETE FROM provider_hourly_stats WHERE provider = 'utcwin-p'")
        return [row["hour_bucket"] for row in rows]

    results = await _each_zone(_bucket)

    assert results == {zone: [hour] for zone in ZONES}
