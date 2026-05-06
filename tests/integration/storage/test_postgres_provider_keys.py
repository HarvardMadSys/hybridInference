"""Integration tests for the provider_api_keys CRUD methods (Postgres)."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"


@pytest_asyncio.fixture
async def postgres_op_store():
    """Provide a clean PostgresOperationalStore for provider_api_keys tests."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "freeinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }

    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

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
