"""The coalesced cost-counter write and the debounced ``last_used_at`` against real SQL.

The spend ledger hands the store one batch per write, summed per user, key
and UTC day. What the counters end up holding must be exactly what the
separate per-request increments would have left -- the same dollars to the
counter's six places, the same request counts -- with the account's and the
key's counters written together.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from serving.spend_ledger import counted_amount
from serving.storage.base import CostIncrement
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

DAY = "2026-11-02"
# Costs with more places than the counters keep: each request rounds on its own.
COSTS = [0.0000123456, 0.0000025, 0.25, 0.0004999, 0.1000005, 0.0000004]


@pytest_asyncio.fixture
async def counters():
    pool = await postgres.create_pool(min_size=1, max_size=5)
    store = PostgresOperationalStore(pool)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_key_daily_cost WHERE user_id LIKE 'u-ctr-%'")
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id LIKE 'u-ctr-%'")
            await conn.execute("DELETE FROM api_keys WHERE key_prefix LIKE 'sk-ctr-%'")
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-ctr-%'")

    await _wipe()
    try:
        yield store, pool
    finally:
        await _wipe()
        await pool.close()


async def _account(pool, user_id: str) -> tuple[str, int]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cost_usd, requests FROM user_daily_cost WHERE user_id = $1 AND day = $2",
            user_id,
            DAY,
        )
    return str(row["cost_usd"]), row["requests"]


async def _key(pool, key_id: int) -> tuple[str, int, str]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cost_usd, requests, user_id FROM api_key_daily_cost "
            "WHERE key_id = $1 AND day = $2",
            key_id,
            DAY,
        )
    return str(row["cost_usd"]), row["requests"], row["user_id"]


async def test_a_coalesced_batch_adds_exactly_what_the_separate_increments_would(counters):
    store, pool = counters
    for cost in COSTS:
        await store.increment_user_cost("u-ctr-separate", cost, day=DAY, api_key_id=9001)

    await store.increment_user_costs(
        [
            CostIncrement(
                "u-ctr-coalesced",
                DAY,
                9002,
                sum(counted_amount(cost) for cost in COSTS),
                len(COSTS),
            )
        ]
    )

    assert await _account(pool, "u-ctr-coalesced") == await _account(pool, "u-ctr-separate")
    assert (await _key(pool, 9002))[:2] == (await _key(pool, 9001))[:2]
    assert (await _account(pool, "u-ctr-coalesced"))[1] == len(COSTS)


async def test_one_batch_writes_many_users_keys_and_days_onto_existing_rows(counters):
    store, pool = counters
    await store.increment_user_cost("u-ctr-a", 1.0, day=DAY, api_key_id=9011)

    await store.increment_user_costs(
        [
            CostIncrement("u-ctr-a", DAY, 9011, counted_amount(0.5), 2),
            CostIncrement("u-ctr-a", DAY, 9012, counted_amount(0.25), 1),
            CostIncrement("u-ctr-a", DAY, None, counted_amount(2.0), 1),
            CostIncrement("u-ctr-b", DAY, None, counted_amount(0.125), 3),
            CostIncrement("u-ctr-b", "2026-11-03", None, counted_amount(4.0), 1),
        ]
    )

    assert await _account(pool, "u-ctr-a") == ("3.750000", 5)
    assert await _key(pool, 9011) == ("1.500000", 3, "u-ctr-a")
    assert await _key(pool, 9012) == ("0.250000", 1, "u-ctr-a")
    assert await _account(pool, "u-ctr-b") == ("0.125000", 3)
    async with pool.acquire() as conn:
        next_day = await conn.fetchval(
            "SELECT cost_usd FROM user_daily_cost WHERE user_id = 'u-ctr-b' AND day = '2026-11-03'"
        )
    assert str(next_day) == "4.000000"


async def test_a_committed_transaction_reads_back_as_committed(counters):
    """The status query works on this server (its functions and types)."""
    store, pool = counters
    async with pool.acquire() as conn, conn.transaction():
        xid = await conn.fetchval("SELECT txid_current()")

    assert await store._transaction_status(xid) == "committed"


async def test_last_used_is_written_at_most_once_a_minute(counters):
    store, pool = counters
    await store.create_user(user_id="u-ctr-used", email="u-ctr-used@example.com", password_hash="x")
    async with pool.acquire() as conn:
        key_id = await conn.fetchval(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, status) "
            "VALUES ('h-ctr-used', 'sk-ctr-used', 'u-ctr-used', 'active') RETURNING id"
        )

    async def last_used():
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT last_used_at FROM api_keys WHERE id = $1", key_id)

    assert await last_used() is None
    await store.update_key_last_used(key_id)
    first = await last_used()
    assert first is not None

    await store.update_key_last_used(key_id)
    assert await last_used() == first  # within the minute: left alone

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET last_used_at = NOW() - INTERVAL '61 seconds' WHERE id = $1",
            key_id,
        )
    stale = await last_used()
    await store.update_key_last_used(key_id)
    assert await last_used() > stale
