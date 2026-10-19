"""PostgreSQL integration test for the Users tab's provider filter choices.

``list_user_activity_providers`` used a ``SELECT DISTINCT`` over the last N days
of ``api_logs``; it is now a skip scan of ``idx_api_logs_provider``. This runs
both on the same rows -- the old query kept verbatim as the oracle -- with
providers on each seam: empty, NULL, only outside the window, only just inside
it, sharing a prefix, and differing only in case.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from serving.storage.log_schema import ensure_api_logs_schema
from serving.storage.postgres_operational import (
    ACTIVITY_PROVIDERS_SQL,
    PostgresOperationalStore,
)
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

# What list_user_activity_providers ran before, verbatim.
_ORACLE_SQL = (
    "SELECT DISTINCT provider FROM api_logs "
    "WHERE provider IS NOT NULL AND provider <> '' "
    "AND timestamp >= NOW() - make_interval(days => $1) "
    "ORDER BY provider"
)


@pytest_asyncio.fixture
async def db():
    schema = "user_providers_" + uuid.uuid4().hex
    admin = await postgres.connect()
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await postgres.create_pool(
        min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            await ensure_api_logs_schema(conn)
        yield pool, PostgresOperationalStore(pool)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _log(conn, provider, age, *, null_timestamp=False) -> None:
    """One row from *provider*, *age* (an interval string) ago."""
    # ``provider`` is NOT NULL in the schema; rows predating that, or written
    # by another tool, are what the oracle's NULL test guards against, so the
    # NULL case drops the constraint for this schema.
    await conn.execute(
        "INSERT INTO api_logs (request_id, model_id, provider, timestamp) "
        "VALUES ($1, 'm', $2, CASE WHEN $4 THEN NULL ELSE NOW() - $3::text::interval END)",
        uuid.uuid4().hex,
        provider,
        age,
        null_timestamp,
    )


async def _both(pool, store, days):
    async with pool.acquire() as conn:
        oracle = [r["provider"] for r in await conn.fetch(_ORACLE_SQL, days)]
    return oracle, await store.list_user_activity_providers(days=days)


async def test_matches_the_distinct_scan_on_every_seam(db):
    pool, store = db
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE api_logs ALTER COLUMN provider DROP NOT NULL")
        rows = [
            ("openrouter", "1 hour"),
            ("openrouter", "40 days"),
            ("deepseek", "29 days 23 hours"),  # just inside 30 days
            ("retired", "31 days"),  # only outside 30 days
            ("retired", "400 days"),
            ("zai", "2 days"),
            ("zai-coding", "3 days"),  # shares a prefix
            ("ZAI", "5 days"),  # differs only in case
            ("", "1 hour"),  # empty
            (None, "1 hour"),  # missing
            ("aaa-first", "100 days"),  # sorts first, outside the window
        ]
        for provider, age in rows:
            await _log(conn, provider, age)
        await _log(conn, "no-timestamp", "0", null_timestamp=True)

    for days in (30, 1, 365, 1000):
        oracle, skip_scan = await _both(pool, store, days)
        assert skip_scan == oracle, days
    oracle, _ = await _both(pool, store, 30)
    assert "deepseek" in oracle and "retired" not in oracle


async def test_an_empty_table_has_no_providers(db):
    pool, store = db
    assert await _both(pool, store, 30) == ([], [])


async def test_reads_one_index_probe_per_provider(db):
    """The plan walks the provider index; it never scans the window's rows."""
    pool, _ = db
    async with pool.acquire() as conn:
        # Enough rows that a scan would be visible in the buffer counts.
        await conn.execute(
            "INSERT INTO api_logs (request_id, model_id, provider, timestamp) "
            "SELECT 'bulk-' || g, 'm', 'p' || (g % 5), NOW() - (g % 20) * interval '1 day' "
            "FROM generate_series(1, 50000) g"
        )
        await conn.execute("ANALYZE api_logs")
        plan = await conn.fetchval("EXPLAIN (ANALYZE, FORMAT JSON) " + ACTIVITY_PROVIDERS_SQL, 30)
    text = str(plan)
    assert "Seq Scan" not in text
    assert "idx_api_logs_provider" in text
