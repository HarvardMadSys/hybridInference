"""Integration tests for the provider_api_keys CRUD methods (Postgres)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def postgres_op_store():
    """Provide a clean PostgresOperationalStore for provider_api_keys tests."""
    pool = await postgres.create_pool(min_size=1, max_size=5)

    store = PostgresOperationalStore(pool)
    await store.initialize()

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM provider_api_keys")

    try:
        yield store
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM provider_api_keys")
        await pool.close()


async def test_provider_keys_round_trip(postgres_op_store: PostgresOperationalStore):
    """add → list → list_full → delete cycle behaves as documented."""
    raw_key = "sk-zai-integration-1234567890abcdef"
    key_id = await postgres_op_store.add_provider_key(
        provider="zai",
        api_key=raw_key,
        label="ops-test",
        created_by=None,
    )
    assert key_id

    rows = await postgres_op_store.list_provider_keys("zai")
    assert len(rows) == 1
    row = rows[0]
    assert row.id == key_id
    assert row.provider == "zai"
    assert row.label == "ops-test"
    assert row.status == "active"
    # key_prefix is masked — the raw secret must not appear.
    assert raw_key not in row.key_prefix

    raws = await postgres_op_store.list_provider_keys_full("zai")
    assert raws == [raw_key]

    deleted = await postgres_op_store.delete_provider_key(key_id)
    assert deleted is True

    assert await postgres_op_store.list_provider_keys("zai") == []
    assert await postgres_op_store.list_provider_keys_full("zai") == []
    assert await postgres_op_store.delete_provider_key(key_id) is False
