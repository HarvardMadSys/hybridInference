"""The purge of ended sessions' rows against PostgreSQL, in a schema of its own."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from serving.storage import session_retention
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pool():
    """A pool whose ``auth_sessions`` is a copy of the real table's shape, and empty."""
    schema = "session_retention_" + uuid.uuid4().hex
    admin = await postgres.connect()
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await postgres.create_pool(
        min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE auth_sessions (LIKE public.auth_sessions INCLUDING ALL)"
            )
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


# (id, expires in, revoked, last used, created): intervals relative to NOW().
_SESSIONS = [
    ("live", "30 days", False, "-1 hour", "-3 days"),
    ("live-null-revoked", "30 days", None, None, "-3 days"),
    ("expired-long-ago", "-2 days", False, "-9 days", "-10 days"),
    ("expired-just-now", "-1 hour", False, "-2 hours", "-10 days"),
    ("revoked-long-ago", "20 days", True, "-2 days", "-3 days"),
    ("revoked-just-now", "20 days", True, "-1 hour", "-3 days"),
    ("revoked-never-used", "20 days", True, None, "-2 days"),
    ("revoked-new", "20 days", True, None, "-1 hour"),
]


async def _seed(pool) -> None:
    async with pool.acquire() as conn:
        for sid, expires, revoked, used, created in _SESSIONS:
            await conn.execute(
                "INSERT INTO auth_sessions "
                "(id, user_id, refresh_token_hash, sid, expires_at, revoked, last_used_at, "
                "created_at) VALUES ($1, 'u-ret', 'hash-' || $1, $1, NOW() + $2::text::interval, "
                "$3, NOW() + $4::text::interval, NOW() + $5::text::interval)",
                sid,
                expires,
                revoked,
                used,
                created,
            )


async def _left(pool) -> set[str]:
    async with pool.acquire() as conn:
        return {r["id"] for r in await conn.fetch("SELECT id FROM auth_sessions")}


async def test_only_sessions_that_ended_over_a_day_ago_go(pool):
    await _seed(pool)

    assert await session_retention.purge_ended(pool) == 3

    assert await _left(pool) == {
        "live",
        "live-null-revoked",
        "expired-just-now",
        "revoked-just-now",
        "revoked-new",
    }


async def test_a_backlog_goes_in_bounded_statements_over_runs(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO auth_sessions (id, user_id, refresh_token_hash, sid, expires_at) "
            "SELECT 'old-' || g, 'u-ret', 'hash-old-' || g, 's-' || g, "
            "NOW() - INTERVAL '3 days' FROM generate_series(1, 25) AS g"
        )

    assert await session_retention.purge_ended(pool, batch=10, max_batches=2) == 20
    assert len(await _left(pool)) == 5
    assert await session_retention.purge_ended(pool, batch=10, max_batches=2) == 5
    assert await _left(pool) == set()


async def test_a_purged_session_reads_as_ended(pool):
    """What the purge removes was already not live: the answer does not change."""
    await _seed(pool)
    store = PostgresOperationalStore(pool)
    before = {sid: await store.session_is_live("u-ret", sid) for sid, *_ in _SESSIONS}

    await session_retention.purge_ended(pool)

    after = {sid: await store.session_is_live("u-ret", sid) for sid, *_ in _SESSIONS}
    assert after == before
    assert {sid for sid, live in after.items() if live} == {"live", "live-null-revoked"}
