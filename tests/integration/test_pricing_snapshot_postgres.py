"""A shared tariff is published exactly once across concurrent gateway workers."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.mark.asyncio
async def test_atomic_tariff_insert_and_update_reject_stale_writers():
    schema = f"pricing_sync_{uuid.uuid4().hex}"
    admin = await postgres.connect()
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await postgres.create_pool(
            min_size=2, max_size=3, server_settings={"search_path": schema}
        )
        async with pool.acquire() as connection:
            await connection.execute(
                "CREATE TABLE site_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "value_type TEXT NOT NULL, updated_at TIMESTAMPTZ, updated_by TEXT)"
            )
        store = PostgresOperationalStore(pool)
        key = "provider_pricing:deepseek"
        inserted = await asyncio.gather(
            *(
                store.compare_and_set_setting(key, None, value, "json", "sync")
                for value in ("first-worker", "second-worker")
            )
        )
        assert sorted(inserted) == [False, True]
        previous = (await store.get_setting(key))["value"]
        updated = await asyncio.gather(
            *(
                store.compare_and_set_setting(key, previous, value, "json", "sync")
                for value in ("revision-two-a", "revision-two-b")
            )
        )
        assert sorted(updated) == [False, True]
        winner = (await store.get_setting(key))["value"]
        assert winner in {"revision-two-a", "revision-two-b"}
        assert not await store.compare_and_set_setting(key, previous, "late", "json", "sync")
        assert not await store.compare_and_set_setting(key, None, "late-insert", "json", "sync")
        assert (await store.get_setting(key))["value"] == winner
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
