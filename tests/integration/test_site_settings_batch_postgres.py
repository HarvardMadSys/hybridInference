"""Several runtime settings are stored together or not at all (PostgreSQL).

``PATCH /admin/settings`` writes its values through ``set_settings``. Written
one by one, a failure part way left the first values in force while the
administrator was told the whole save had failed.
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest, pytest.mark.asyncio]

KEYS = ("itest_batch_first", "itest_batch_second")


@pytest_asyncio.fixture
async def store():
    pool = await postgres.create_pool(min_size=1, max_size=2)

    async def wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM site_settings WHERE key = ANY($1::text[])", list(KEYS))

    await wipe()
    try:
        yield pool, CachedOperationalStore(PostgresOperationalStore(pool), InMemoryCache())
    finally:
        await wipe()
        await pool.close()


async def _stored(pool) -> dict[str, str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value FROM site_settings WHERE key = ANY($1::text[])", list(KEYS)
        )
    return {row["key"]: row["value"] for row in rows}


async def test_every_value_is_stored_and_updated(store):
    pool, op_store = store
    await op_store.set_setting("itest_batch_first", "1", "int", "itest")

    await op_store.set_settings(
        [("itest_batch_first", "2", "int"), ("itest_batch_second", "true", "bool")], "itest"
    )

    assert await _stored(pool) == {"itest_batch_first": "2", "itest_batch_second": "true"}


async def test_a_row_the_database_refuses_leaves_the_others_unwritten(store):
    pool, op_store = store
    await op_store.set_setting("itest_batch_first", "1", "int", "itest")

    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await op_store.set_settings(
            [("itest_batch_first", "2", "int"), ("itest_batch_second", None, "bool")],  # type: ignore[list-item]
            "itest",
        )

    assert await _stored(pool) == {"itest_batch_first": "1"}
