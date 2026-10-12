"""Upgrading the users table while a reader holds it, against PostgreSQL.

Every account lookup selects ``login_name``. An upgrade that cannot take the
users table's lock (the nightly ``pg_dump`` holds ACCESS SHARE for hours) must
refuse to start rather than serve on a schema where sign-in fails with
``UndefinedColumnError``; once the lock is free the same start succeeds.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from serving.storage.postgres_operational import (
    PostgresOperationalStore,
    RequiredOperationalSchemaUnavailable,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"


async def _columns(conn: asyncpg.Connection) -> dict[str, bool]:
    """Return the users columns this test watches, with whether each is nullable."""
    rows = await conn.fetch(
        "SELECT column_name, is_nullable = 'YES' AS nullable FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name IN ('email', 'login_name')"
    )
    return {row["column_name"]: row["nullable"] for row in rows}


@pytest_asyncio.fixture
async def pool():
    """A pool on the test database, its users table as a release before login names left it."""
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )
    try:
        pool = await asyncpg.create_pool(
            host=os.getenv("TEST_DB_HOST", "localhost"),
            port=int(os.getenv("TEST_DB_PORT", "5432")),
            database=test_db_name,
            user=os.getenv("TEST_DB_USER", "postgres"),
            password=os.getenv("TEST_DB_PASSWORD", "postgres"),
            min_size=1,
            max_size=4,
        )
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    await PostgresOperationalStore(pool).initialize()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE email IS NULL")
        await conn.execute("DROP INDEX IF EXISTS idx_users_login_name")
        await conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS login_name")
        await conn.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL")
    try:
        yield pool
    finally:
        # Leave the current schema behind for the other database tests.
        await PostgresOperationalStore(pool).initialize()
        await pool.close()


async def test_an_upgrade_behind_a_reader_refuses_to_serve_then_completes(pool) -> None:
    store = PostgresOperationalStore(pool)
    reader = await pool.acquire()
    try:
        # What pg_dump does: a long transaction that has read the table.
        transaction = reader.transaction()
        await transaction.start()
        await reader.fetchval("SELECT count(*) FROM users")

        with pytest.raises(RequiredOperationalSchemaUnavailable):
            await store.initialize()

        async with pool.acquire() as conn:
            assert "login_name" not in await _columns(conn)
        await transaction.rollback()
    finally:
        await pool.release(reader)

    await store.initialize()

    async with pool.acquire() as conn:
        assert await _columns(conn) == {"email": True, "login_name": True}
    assert await store.get_user_by_login_name("nobody") is None
