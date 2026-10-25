"""The admin Users filters and the audit log against real SQL.

``list_users`` builds one WHERE clause from many optional filters, each adding
its own numbered placeholders, and the count and the page must apply the same
one. The admin routes are tested with the store mocked; the filters below had
never run against PostgreSQL. Every query here also passes ``search`` for this
file's e-mail domain, so rows other tests leave in the shared database never
count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_DOMAIN = "adml.test"


@pytest_asyncio.fixture
async def admin_store():
    pool = await postgres.create_pool(min_size=1, max_size=5)
    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'req-adml-%'")
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id LIKE 'u-adml-%'")
            await conn.execute("DELETE FROM api_keys WHERE key_prefix LIKE 'sk-adml%'")
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-adml-%'")
            await conn.execute("DELETE FROM admin_audit_log WHERE action LIKE 'itest.%'")

    await _wipe()
    try:
        yield store, pool
    finally:
        await _wipe()
        await pool.close()


async def _user(store: PostgresOperationalStore, name: str, status: str = "active") -> str:
    user_id = f"u-adml-{name}"
    await store.create_user(
        user_id=user_id, email=f"{name}@{_DOMAIN}", password_hash="x", status=status
    )
    return user_id


async def _key(
    pool: Any, user_id: str, quota: Decimal | None, status: str = "active", tag: str = ""
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id, "
            "quota_daily_cost_usd, status) VALUES ($1, $2, $3, $3, $4, $5)",
            f"hash-{user_id}{tag}",
            f"sk-adml-{user_id[7:]}{tag}",
            user_id,
            quota,
            status,
        )


_seq = iter(range(10**6))


async def _spend(pool: Any, user_id: str, cost: str, at: datetime) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_logs (request_id, model_id, provider, timestamp, user_id, cost_usd) "
            "VALUES ($1, 'm', 'p', $2, $3, $4)",
            f"req-adml-{next(_seq)}",
            at,
            user_id,
            Decimal(cost),
        )


async def _daily(pool: Any, user_id: str, days_ago: int, cost: str) -> None:
    day = (datetime.now(UTC).date() - timedelta(days=days_ago)).isoformat()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests) VALUES ($1, $2, $3, 1)",
            user_id,
            day,
            Decimal(cost),
        )


async def _listed(store: PostgresOperationalStore, **filters: Any) -> tuple[int, list[str]]:
    total, rows, _counts = await store.list_users(search=_DOMAIN, **filters)
    return total, [row["id"].removeprefix("u-adml-") for row in rows]


def _now() -> datetime:
    return datetime.now(UTC)


def _start_of_month() -> datetime:
    now = _now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def test_quota_state_default_and_custom_read_the_active_key(admin_store):
    store, pool = admin_store
    await _key(pool, await _user(store, "inherits"), None)
    await _key(pool, await _user(store, "custom"), Decimal("5"))
    await _user(store, "keyless")
    # Only the live key decides: an old revoked key without a quota does not.
    switched = await _user(store, "switched")
    await _key(pool, switched, None, status="revoked", tag="-old")
    await _key(pool, switched, Decimal("5"))

    assert await _listed(store, quota_state="default") == (1, ["inherits"])
    total, custom = await _listed(store, quota_state="custom")
    assert (total, sorted(custom)) == (2, ["custom", "switched"])


async def test_near_and_over_compare_todays_spend_with_the_daily_quota(admin_store):
    """>= 80% is near, >= 100% over; yesterday's spend and an unset quota count for nothing."""
    store, pool = admin_store
    for name, quota, today, yesterday in [
        ("under", "10", "7.99", "0"),
        ("near", "10", "8.00", "0"),
        ("over", "10", "10.00", "0"),
        ("yesterday", "10", "0", "50"),
        ("unlimited", None, "500", "0"),
    ]:
        user_id = await _user(store, name)
        await _key(pool, user_id, Decimal(quota) if quota else None)
        if today != "0":
            await _spend(pool, user_id, today, _now())
        if yesterday != "0":
            await _spend(pool, user_id, yesterday, _now() - timedelta(days=1, hours=1))

    _, near = await _listed(store, quota_state="near")
    _, over = await _listed(store, quota_state="over")

    assert sorted(near) == ["near", "over"]
    assert over == ["over"]


async def test_min_cost_month_counts_only_this_utc_month(admin_store):
    store, pool = admin_store
    this_month = await _user(store, "thismonth")
    last_month = await _user(store, "lastmonth")
    await _spend(pool, this_month, "3", _start_of_month() + timedelta(seconds=1))
    await _spend(pool, this_month, "2", _now())
    await _spend(pool, last_month, "100", _start_of_month() - timedelta(seconds=1))

    assert await _listed(store, min_cost_month=Decimal("5")) == (1, ["thismonth"])
    assert await _listed(store, min_cost_month=Decimal("5.01")) == (0, [])


async def test_anomaly_needs_history_a_dollar_and_five_times_the_average(admin_store):
    store, pool = admin_store
    cases = {
        # name: (status, today, prior days' costs)
        "spike": ("active", "10", ["1", "2", "3"]),  # avg 2 -> 10 >= 5x
        "steady": ("active", "9.99", ["2", "2", "2"]),  # 9.99 < 5x2
        "new": ("active", "10", ["1", "1"]),  # two days of history only
        "cheap": ("active", "0.99", ["0.01", "0.01", "0.01"]),  # under $1 today
        "suspended": ("suspended", "10", ["1", "1", "1"]),  # active users only
    }
    for name, (status, today, prior) in cases.items():
        user_id = await _user(store, name, status=status)
        await _daily(pool, user_id, 0, today)
        for days_ago, cost in enumerate(prior, start=1):
            await _daily(pool, user_id, days_ago, cost)
    # Older than the 7-day window: must not count as history.
    await _daily(pool, "u-adml-new", 8, "1")

    assert await _listed(store, anomaly=True) == (1, ["spike"])


async def test_cost_sorts_order_the_whole_filtered_set(admin_store):
    store, pool = admin_store
    low = await _user(store, "low")
    high = await _user(store, "high")
    await _user(store, "idle")
    await _spend(pool, low, "1", _now())
    await _spend(pool, low, "50", _start_of_month() - timedelta(days=40))
    await _spend(pool, high, "20", _now())

    _, by_month = await _listed(store, sort_by="cost_month")
    total, by_alltime, _ = await store.list_users(search=_DOMAIN, sort_by="cost_alltime", limit=1)

    assert by_month[:2] == ["high", "low"]
    assert total == 3
    # All time, the old spend puts "low" first even on a one-row page.
    assert [row["id"] for row in by_alltime] == [low]
    assert by_alltime[0]["usage_alltime"] == Decimal("51")


async def test_filters_combine_and_the_count_matches_the_page(admin_store):
    """Filters that each add placeholders, together, with a page smaller than the total."""
    store, pool = admin_store
    for index in range(3):
        user_id = await _user(store, f"combo{index}")
        await _key(pool, user_id, Decimal("10"))
        await _spend(pool, user_id, "9", _now())
    await _key(pool, await _user(store, "combo-suspended", status="suspended"), Decimal("10"))

    total, page = await _listed(
        store,
        status="active",
        quota_state="near",
        min_cost_today=Decimal("9"),
        min_cost_month=Decimal("9"),
        limit=2,
        offset=1,
    )

    assert total == 3
    assert len(page) == 2 and set(page) <= {"combo0", "combo1", "combo2"}


# --- audit log --------------------------------------------------------------


async def test_audit_log_filters_by_action_and_target_and_pages_newest_first(admin_store):
    store, _pool = admin_store
    for action, target in [
        ("itest.suspend", "u-adml-a"),
        ("itest.suspend", "u-adml-b"),
        ("itest.approve", "u-adml-a"),
        ("itest.suspend", "u-adml-a"),
    ]:
        await store.log_admin_action(admin_ip="203.0.113.9", action=action, target_user_id=target)

    total, rows = await store.list_audit_log(action="itest.suspend", target_user_id="u-adml-a")
    assert total == 2
    assert {(row["action"], row["target_user_id"]) for row in rows} == {
        ("itest.suspend", "u-adml-a")
    }

    total, first = await store.list_audit_log(action="itest.suspend", limit=1)
    _, rest = await store.list_audit_log(action="itest.suspend", limit=5, offset=1)
    assert total == 3 and len(first) == 1 and len(rest) == 2
    stamps = [row["timestamp"] for row in first + rest]
    assert stamps == sorted(stamps, reverse=True)

    total, rows = await store.list_audit_log(target_user_id="u-adml-b")
    assert (total, [row["action"] for row in rows]) == (1, ["itest.suspend"])
