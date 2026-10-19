"""Real SQL coverage for deferred lifetime totals and server-side user ordering."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from serving.storage.log_schema import ensure_api_logs_schema
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest_asyncio.fixture
async def store():
    schema = "admin_usage_" + uuid.uuid4().hex
    conn = await postgres.connect()
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    pool = await postgres.create_pool(
        min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as db:
            for table in ("users", "api_keys", "user_daily_cost"):
                await db.execute(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
            # api_logs with the daily usage rollup the totals read.
            await ensure_api_logs_schema(db)
            await db.execute("""
                INSERT INTO users (id, email, password_hash, status, created_at)
                VALUES ('empty', 'empty@example.test', 'fixture', 'active', '2026-01-03'),
                       ('many', 'many@example.test', 'fixture', 'active', '2026-01-02'),
                       ('large', 'large@example.test', 'fixture', 'suspended', '2026-01-01')
            """)
            await db.execute("""
                INSERT INTO api_logs (request_id, model_id, provider, user_id, total_tokens, cost_usd)
                VALUES ('a', 'fixture', 'fixture', 'many', 10, 0),
                       ('b', 'fixture', 'fixture', 'many', 20, 0),
                       ('c', 'fixture', 'fixture', 'many', NULL, 1),
                       ('d', 'fixture', 'fixture', 'large', 999, 0)
            """)
        yield PostgresOperationalStore(pool)
    finally:
        await pool.close()
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


async def test_lightweight_and_bulk_match_default_including_free_requests(store):
    total, baseline, counts = await store.list_users()
    light_total, lightweight, light_counts = await store.list_users(include_usage_totals=False)
    assert total == light_total == 3
    assert counts == light_counts
    totals = await store.get_bulk_user_usage_totals([r["id"] for r in lightweight])
    assert totals == {
        "empty": {"requests": 0, "tokens": 0},
        "many": {"requests": 3, "tokens": 30},
        "large": {"requests": 1, "tokens": 999},
    }
    for before, after in zip(baseline, lightweight, strict=True):
        assert after["usage_alltime_requests"] is None
        assert after["usage_alltime_tokens"] is None
        after["usage_alltime_requests"] = totals[after["id"]]["requests"]
        after["usage_alltime_tokens"] = totals[after["id"]]["tokens"]
        assert before == after


@pytest.mark.parametrize(
    "sort_by,expected", [("requests", ["many", "large"]), ("tokens", ["large", "many"])]
)
async def test_usage_sort_applies_before_pagination_and_respects_filters(store, sort_by, expected):
    for offset, uid in enumerate(expected):
        total, rows, _ = await store.list_users(
            sort_by=sort_by, limit=1, offset=offset, include_usage_totals=False
        )
        assert total == 3
        assert [r["id"] for r in rows] == [uid]
    total, rows, _ = await store.list_users(
        sort_by=sort_by, status="active", limit=1, include_usage_totals=False
    )
    assert total == 2
    assert [r["id"] for r in rows] == ["many"]
