"""The merged auth+spend lookup (issue #185) answers against real Postgres."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def postgres_op_store():
    """Provide a bare PostgresOperationalStore pointed at the test database."""
    pool = await postgres.create_pool(min_size=1, max_size=5)

    store = PostgresOperationalStore(pool)
    await store.initialize()

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM api_keys WHERE key_prefix LIKE 'sk-aspend-%'")
        await conn.execute("DELETE FROM users WHERE id LIKE 'u-aspend-%'")

    try:
        yield store
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_keys WHERE key_prefix LIKE 'sk-aspend-%'")
            await conn.execute("DELETE FROM users WHERE id LIKE 'u-aspend-%'")
        await pool.close()


async def _seed_user_with_key(
    store: PostgresOperationalStore, *, user_id: str, email: str, status: str = "active"
) -> None:
    await store.create_user(user_id=user_id, email=email, password_hash="x")
    await store.create_key(
        key_hash=f"hash-{user_id}",
        key_prefix=f"sk-aspend-{user_id}",
        user_id=user_id,
        account_id=user_id,
        quota_daily_cost_usd=Decimal("100.00"),
    )
    if status != "active":
        await store.update_key(user_id, status=status)


async def _set_spend(pool, user_id: str, cost: str) -> None:
    """Write today's counter row the way the increment path would."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd) "
            "VALUES ($1, to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD'), $2::numeric) "
            "ON CONFLICT (user_id, day) DO UPDATE SET cost_usd = EXCLUDED.cost_usd",
            user_id,
            Decimal(cost),
        )


async def test_identity_and_spend_arrive_together(
    postgres_op_store: PostgresOperationalStore,
):
    await _seed_user_with_key(postgres_op_store, user_id="u-aspend-1", email="aspend1@x.com")
    await _set_spend(postgres_op_store._pool, "u-aspend-1", "12.5")

    row = await postgres_op_store.get_auth_context_with_spend("hash-u-aspend-1")

    assert row is not None
    assert row["user_id"] == "u-aspend-1"
    assert row["role"] == "free"
    assert float(row["cost_usd"]) == pytest.approx(12.5)


async def test_missing_counter_row_reads_as_zero(
    postgres_op_store: PostgresOperationalStore,
):
    await _seed_user_with_key(postgres_op_store, user_id="u-aspend-2", email="aspend2@x.com")

    row = await postgres_op_store.get_auth_context_with_spend("hash-u-aspend-2")

    assert row is not None
    assert float(row["cost_usd"]) == 0.0


async def test_inactive_key_resolves_to_none(
    postgres_op_store: PostgresOperationalStore,
):
    await _seed_user_with_key(
        postgres_op_store, user_id="u-aspend-3", email="aspend3@x.com", status="revoked"
    )

    row = await postgres_op_store.get_auth_context_with_spend("hash-u-aspend-3")

    assert row is None


async def test_matches_the_two_query_answers_exactly(
    postgres_op_store: PostgresOperationalStore,
):
    """The merged row agrees field-for-field with the lookups it replaces."""
    await _seed_user_with_key(postgres_op_store, user_id="u-aspend-4", email="aspend4@x.com")
    await _set_spend(postgres_op_store._pool, "u-aspend-4", "3.25")

    merged = await postgres_op_store.get_auth_context_with_spend("hash-u-aspend-4")
    identity = await postgres_op_store.get_auth_context_by_key_hash("hash-u-aspend-4")
    spend = await postgres_op_store.get_user_cost_today("u-aspend-4")

    assert merged is not None and identity is not None
    for key in identity:
        assert merged[key] == identity[key], f"{key} differs from the identity lookup"
    assert float(merged["cost_usd"]) == pytest.approx(spend)
