"""A password change signs every other session of the account out."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def store():
    """Use a disposable schema, never another test's or deployment's rows."""
    import asyncpg

    database = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    if "_test_" not in database:
        pytest.fail(f"SAFETY: refusing non-test database {database!r}")
    config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": database,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
    try:
        admin = await asyncpg.connect(**config, timeout=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
    schema = f"revoke_sessions_test_{uuid.uuid4().hex}"
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(
            **config, min_size=1, max_size=3, server_settings={"search_path": schema}
        )
        operational = PostgresOperationalStore(pool)
        await operational.initialize()
        yield operational
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def session(store, user_id: str, sid: str) -> None:
    await store.create_session(
        session_id=f"{user_id}-{sid}",
        user_id=user_id,
        refresh_token_hash=uuid.uuid4().hex,
        jti=uuid.uuid4().hex,
        sid=sid,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )


async def revoked(store) -> dict[str, bool]:
    async with store._pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, revoked FROM auth_sessions ORDER BY id")
    return {row["id"]: row["revoked"] for row in rows}


async def test_every_session_but_the_current_one_is_revoked(store):
    for sid in ("here", "laptop", "phone"):
        await session(store, "owner", sid)
    await session(store, "someone-else", "laptop")

    assert await store.revoke_user_sessions("owner", except_sid="here") == 2
    assert await revoked(store) == {
        "owner-here": False,
        "owner-laptop": True,
        "owner-phone": True,
        "someone-else-laptop": False,
    }
    # Already-revoked sessions are not counted again.
    assert await store.revoke_user_sessions("owner", except_sid="here") == 0


async def test_without_a_current_session_every_session_is_revoked(store):
    for sid in ("laptop", "phone"):
        await session(store, "owner", sid)
    assert await store.revoke_user_sessions("owner") == 2
    assert set((await revoked(store)).values()) == {True}
