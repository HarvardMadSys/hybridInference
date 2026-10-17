"""``python -m serving.auth.reset_password`` against PostgreSQL, before and after the upgrade.

The command is how an operator gets back in, including while another session
holds the users table and the upgraded backend's users migration cannot run
yet. It must work on that database as it is, and on the current schema.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from serving.auth import reset_password as cli
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.utils import password as password_utils

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
NEW_PASSWORD = "Rec0veredPassword"
OLD_USER = "u-reset-cli-old"
ADMIN_USER = "u-reset-cli-admin"


def _test_database() -> dict[str, str]:
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in name:
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )
    return {
        "DB_HOST": os.getenv("TEST_DB_HOST", "localhost"),
        "DB_PORT": os.getenv("TEST_DB_PORT", "5432"),
        "DB_NAME": name,
        "DB_USER": os.getenv("TEST_DB_USER", "postgres"),
        "DB_PASSWORD": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


async def _forget(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "DELETE FROM auth_sessions WHERE user_id = ANY($1::text[])", [OLD_USER, ADMIN_USER]
    )
    await conn.execute("DELETE FROM users WHERE id = ANY($1::text[])", [OLD_USER, ADMIN_USER])


@pytest_asyncio.fixture
async def database(monkeypatch, tmp_path):
    """A pool on the current schema; the command reaches the same database through ``DB_*``."""
    settings = _test_database()
    try:
        pool = await asyncpg.create_pool(
            host=settings["DB_HOST"],
            port=int(settings["DB_PORT"]),
            database=settings["DB_NAME"],
            user=settings["DB_USER"],
            password=settings["DB_PASSWORD"],
            min_size=1,
            max_size=4,
        )
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)  # the command must not find a .env

    await PostgresOperationalStore(pool).initialize()
    async with pool.acquire() as conn:
        await _forget(conn)
    try:
        yield pool
    finally:
        # Leave the current schema behind for the other database tests.
        await PostgresOperationalStore(pool).initialize()
        async with pool.acquire() as conn:
            await _forget(conn)
        await pool.close()


async def _columns(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = 'users'::regclass AND attnum > 0 AND NOT attisdropped"
    )
    return {row["attname"] for row in rows}


async def _add_session(conn: asyncpg.Connection, user_id: str) -> None:
    await conn.execute(
        "INSERT INTO auth_sessions (id, user_id, refresh_token_hash, sid, expires_at) "
        "VALUES ($1, $2, $3, 'sid', NOW() + INTERVAL '1 day')",
        f"session-{user_id}",
        user_id,
        f"refresh-hash-{user_id}",
    )


async def _password_hash_and_sessions(conn: asyncpg.Connection, user_id: str) -> tuple[str, int]:
    password_hash = await conn.fetchval("SELECT password_hash FROM users WHERE id = $1", user_id)
    sessions = await conn.fetchval("SELECT count(*) FROM auth_sessions WHERE user_id = $1", user_id)
    return password_hash, sessions


async def test_before_the_users_migration_while_a_reader_holds_the_table(database):
    pool = database
    async with pool.acquire() as conn:
        # The users table as a release before login names and phone numbers left it.
        await conn.execute("DROP INDEX IF EXISTS idx_users_login_name")
        await conn.execute("DROP INDEX IF EXISTS idx_users_phone_unique")
        await conn.execute(
            "ALTER TABLE users DROP COLUMN IF EXISTS login_name, "
            "DROP COLUMN IF EXISTS phone, DROP COLUMN IF EXISTS phone_verified"
        )
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, user_name, status) "
            "VALUES ($1, 'old-reset@example.com', 'old-hash', 'Old', 'active')",
            OLD_USER,
        )
        await _add_session(conn, OLD_USER)
    # What a store that has not read the catalog does on this database; the
    # command never initializes one.
    with pytest.raises(asyncpg.UndefinedColumnError):
        await PostgresOperationalStore(pool).get_user_by_email("old-reset@example.com")

    # A long reader, as the nightly pg_dump: the upgrade's ALTER TABLE cannot
    # get its lock, so the backend defers its users migration, and the reset
    # must not need that lock either.
    reader = await pool.acquire()
    transaction = reader.transaction()
    await transaction.start()
    try:
        await reader.fetchval("SELECT count(*) FROM users")

        account = await cli._reset_with_database("Old-Reset@Example.com", NEW_PASSWORD)

        assert (account["id"], account["email"], account["status"]) == (
            OLD_USER,
            "old-reset@example.com",
            "active",
        )
        with pytest.raises(cli.ResetPasswordError, match="no login names yet"):
            await cli._reset_with_database("admin", NEW_PASSWORD)
    finally:
        await transaction.rollback()
        await pool.release(reader)

    async with pool.acquire() as conn:
        password_hash, sessions = await _password_hash_and_sessions(conn, OLD_USER)
        assert password_utils.verify_password(NEW_PASSWORD, password_hash)
        assert sessions == 0
        # The command migrated nothing.
        assert not {"login_name", "phone", "phone_verified"} & await _columns(conn)


async def test_the_first_run_administrator_by_login_name(database):
    pool = database
    async with pool.acquire() as conn:
        # The row first-run setup creates: a login name and no email address.
        await conn.execute(
            "INSERT INTO users (id, email, login_name, password_hash, user_name, role, "
            "status, email_verified) "
            "VALUES ($1, NULL, 'resetadmin', 'old-hash', 'Reset Admin', 'admin', 'active', TRUE)",
            ADMIN_USER,
        )
        await _add_session(conn, ADMIN_USER)

    account = await cli._reset_with_database(" RESETADMIN ", NEW_PASSWORD)

    assert (account["id"], account["email"], account["login_name"]) == (
        ADMIN_USER,
        None,
        "resetadmin",
    )
    assert cli.describe_reset(account, None)[0] == (
        f"Password reset for resetadmin (user id {ADMIN_USER})."
    )
    async with pool.acquire() as conn:
        password_hash, sessions = await _password_hash_and_sessions(conn, ADMIN_USER)
    assert password_utils.verify_password(NEW_PASSWORD, password_hash)
    assert sessions == 0
