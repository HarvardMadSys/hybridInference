"""Upgrading the users table while a reader holds it, against PostgreSQL.

The first boot after an upgrade often meets the nightly ``pg_dump``, which holds
ACCESS SHARE on every table for hours, while adding a column needs ACCESS
EXCLUSIVE. The operational store must start anyway: it defers the account
migration (login names, phone numbers, accounts without an email), reads only
the columns that exist, refuses the writes that need the new ones with
``AccountSchemaPending``, and lands the migration once a retry finds the lock
free. Refusing to start instead crash-looped the backend until the backup ended.

Each test builds the ``users`` and ``login_events`` tables of the release before
#1515 (``git show e6eb9a17~1:apps/backend/serving/storage/postgres_operational.py``)
in a schema of its own, so the shared test database's tables are not touched.
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from serving.exceptions import AccountSchemaPending
from serving.servers import bootstrap
from serving.storage import account_schema, log_schema
from serving.storage.log_schema import (
    SchemaLockUnavailable,
    ensure_api_logs_schema,
    existing_columns,
)
from serving.storage.postgres_operational import (
    PostgresOperationalStore,
    RequiredOperationalSchemaUnavailable,
)
from tests.fixtures import postgres

if TYPE_CHECKING:
    import asyncpg

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ACCOUNT_COLUMNS = {"login_name", "phone", "phone_verified"}


@pytest.fixture(autouse=True)
def _short_lock_wait(monkeypatch):
    """Time out on the held lock in a fraction of the production bound.

    Every test here holds the lock for as long as it wants the migration to
    defer, so the timeout always fires and its length decides nothing but how
    long the file runs: at the production 3s it was ~25s, the longest file in
    the database suite and so the floor of a parallel run.
    """
    monkeypatch.setattr(log_schema, "_DDL_LOCK_TIMEOUT", "300ms")


_PRE_UPGRADE_USERS = """
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        user_name TEXT,
        preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
        role TEXT NOT NULL DEFAULT 'free'
            CHECK (role IN ('free', 'pro', 'internal', 'admin')),
        email_verified BOOLEAN DEFAULT FALSE,
        status TEXT DEFAULT 'active'
            CHECK (status IN ('active', 'suspended', 'deleted',
                              'pending_approval', 'rejected')),
        hard_delete_pending BOOLEAN NOT NULL DEFAULT FALSE,
        hard_delete_claim_token TEXT,
        approval_note TEXT,
        reviewed_at TIMESTAMPTZ,
        reviewed_by TEXT,
        signup_reason TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        last_login_at TIMESTAMPTZ,
        max_concurrent_requests INT DEFAULT NULL,
        admin_note TEXT,
        suspension_message TEXT
    )
"""

_PRE_UPGRADE_LOGIN_EVENTS = """
    CREATE TABLE login_events (
        id BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        user_id TEXT,
        email TEXT NOT NULL,
        outcome TEXT NOT NULL
            CHECK (outcome IN ('success', 'failure')),
        failure_reason TEXT,
        ip TEXT,
        user_agent TEXT
    )
"""

# As the boot-time logger (``database.py``) creates it, before the operational
# store runs; the store's own copy of the DDL has no ``api_key_encrypted``.
_BOOT_TIME_API_KEYS = """
    CREATE TABLE api_keys (
        id BIGSERIAL PRIMARY KEY,
        key_hash TEXT NOT NULL UNIQUE,
        api_key_encrypted TEXT,
        key_prefix TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        quota_daily_cost_usd DECIMAL(10, 4) DEFAULT 1000.00,
        quota_monthly_cost_usd DECIMAL(10, 4),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        expires_at TIMESTAMPTZ,
        last_used_at TIMESTAMPTZ,
        notes TEXT,
        metadata JSONB,
        account_id TEXT
    )
"""

_ADMIN = {
    "user_id": "u-admin",
    "login_name": "Admin",
    "password_hash": "argon2-hash",
    "user_name": "Ops",
    "marker_key": "setup_completed_at",
    "marker_value": "2026-10-16T00:00:00+00:00",
    "code_key": "setup_code",
    "admin_ip": "203.0.113.7",
}


@pytest_asyncio.fixture
async def pool(monkeypatch):
    """A pool whose search path is a fresh schema holding the pre-#1515 tables."""
    # The store publishes what it reads for SQL outside it; leave the process
    # with the complete schema whatever happens below.
    monkeypatch.setattr(account_schema, "_current", account_schema.COMPLETE)
    admin = await postgres.connect()
    schema = f"users_upgrade_{secrets.token_hex(4)}"
    await admin.execute(f"CREATE SCHEMA {schema}")
    pool = await postgres.create_pool(
        min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            await ensure_api_logs_schema(conn)  # the admin user list reads it
            await conn.execute(_BOOT_TIME_API_KEYS)
            await conn.execute(_PRE_UPGRADE_USERS)
            await conn.execute(_PRE_UPGRADE_LOGIN_EVENTS)
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, user_name, status, email_verified) "
                "VALUES ('u-old', 'old@example.com', 'old-hash', 'Old', 'active', TRUE)"
            )
        yield pool
    finally:
        await pool.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


@asynccontextmanager
async def _held_like_pg_dump(pool: asyncpg.Pool):
    """Hold ACCESS SHARE on every table from another session, as ``pg_dump`` does all run."""
    reader = await pool.acquire()
    transaction = reader.transaction()
    await transaction.start()
    try:
        tables = await reader.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )
        assert {"users", "login_events"} <= {row["tablename"] for row in tables}
        names = ", ".join(row["tablename"] for row in tables)
        await reader.execute(f"LOCK TABLE {names} IN ACCESS SHARE MODE")
        yield
    finally:
        await transaction.rollback()
        await pool.release(reader)


async def _users_columns(pool: asyncpg.Pool) -> set[str]:
    async with pool.acquire() as conn:
        return await existing_columns(conn, "users")


async def _start_deferred(store: PostgresOperationalStore) -> None:
    """Run the first boot's initialize, which must defer rather than refuse."""
    with pytest.raises(SchemaLockUnavailable) as excinfo:
        await store.initialize()
    assert not isinstance(excinfo.value, RequiredOperationalSchemaUnavailable)


async def test_an_upgrade_behind_pg_dump_serves_then_completes(pool) -> None:
    store = PostgresOperationalStore(pool)
    expires = datetime.now(timezone.utc) + timedelta(days=1)

    async with _held_like_pg_dump(pool):
        await _start_deferred(store)
        assert not _ACCOUNT_COLUMNS & await _users_columns(pool)
        async with pool.acquire() as conn:
            for table in ("browser_sessions", "browser_session_accounts", "phone_auth_codes"):
                assert await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table), table

        # Email sign-in, the auth dependency and token refresh.
        user = await store.get_user_by_email("Old@Example.com")
        assert user is not None and user["id"] == "u-old"
        assert (user["login_name"], user["phone"], user["phone_verified"]) == (None, None, False)
        assert (await store.get_user_by_id("u-old"))["email"] == "old@example.com"
        await store.create_session(
            session_id="s-old",
            user_id="u-old",
            refresh_token_hash="refresh-old",
            jti="jti-old",
            sid="sid-old",
            expires_at=expires,
        )
        await store.create_browser_session(
            browser_session_id="b-old", secret_hash="cookie-old", expires_at=expires
        )
        await store.attach_browser_session_account(
            "b-old", user_id="u-old", auth_session_id="s-old"
        )
        [account] = await store.list_browser_session_accounts("b-old")
        assert (account["user_id"], account["login_name"], account["phone"]) == (
            "u-old",
            None,
            None,
        )
        assert (await store.get_session_by_token_hash("refresh-old"))["user_id"] == "u-old"
        await store.update_user_last_login("u-old")
        await store.record_login_event(
            email="old@example.com",
            outcome="success",
            failure_reason=None,
            user_id="u-old",
            ip="203.0.113.7",
            user_agent="pytest",
        )
        # A name that is not an email address finds nobody rather than failing.
        assert await store.get_user_by_login_name("old") is None

        # API keys.
        await store.create_key(key_hash="key-old", key_prefix="sk-old", user_id="u-old")
        context = await store.get_auth_context_by_key_hash("key-old")
        assert (context["email"], context["login_name"], context["phone_verified"]) == (
            "old@example.com",
            None,
            False,
        )
        assert (await store.get_auth_context_lightweight("key-old"))["email"] == "old@example.com"

        # The admin user list, its search and the summary cards.
        total, rows, _ = await store.list_users(search="old")
        assert total == 1 and rows[0]["id"] == "u-old" and rows[0]["login_name"] is None
        assert (await store.get_users_summary())["pending"]["count"] == 0

        # A signup with an email names only columns the old table has.
        await store.create_user(user_id="u-new", email="New@Example.com", password_hash="h")
        assert (await store.get_user_by_email("new@example.com"))["id"] == "u-new"

        # What needs the new columns waits, and says why.
        with pytest.raises(AccountSchemaPending):
            await store.create_user(
                user_id="u-named", email=None, password_hash="h", login_name="named"
            )
        with pytest.raises(AccountSchemaPending):
            await store.require_phone_accounts()
        with pytest.raises(AccountSchemaPending):
            await store.get_user_by_phone("+12025550123")
        with pytest.raises(AccountSchemaPending):
            await store.bind_user_phone("u-old", "+12025550123")

    # The backup ended: bootstrap's background retry calls initialize again.
    await store.initialize()

    assert await _users_columns(pool) >= _ACCOUNT_COLUMNS
    assert store.account_schema == account_schema.COMPLETE
    assert account_schema.current() == account_schema.COMPLETE
    async with pool.acquire() as conn:
        nullable = await conn.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND is_nullable = 'YES' "
            "AND (table_name, column_name) IN "
            "(('users', 'email'), ('users', 'password_hash'), ('login_events', 'email'))"
        )
        assert len(nullable) == 3
    await store.create_user(user_id="u-named", email=None, password_hash="h", login_name="Named")
    assert (await store.get_user_by_login_name(" named "))["id"] == "u-named"
    assert (await store.get_user_by_email("old@example.com"))["phone_verified"] is False
    await store.require_phone_accounts()
    assert await store.bind_user_phone("u-old", "+12025550123")
    assert (await store.get_user_by_phone("+12025550123"))["id"] == "u-old"


async def test_first_run_setup_waits_for_the_upgrade_then_succeeds(pool) -> None:
    """An empty database from before #1515: its administrator needs a login name."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users")
    store = PostgresOperationalStore(pool)

    async with _held_like_pg_dump(pool):
        await _start_deferred(store)
        with pytest.raises(AccountSchemaPending):
            await store.create_first_admin(**_ADMIN)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM users") == 0
            assert await conn.fetchval("SELECT count(*) FROM site_settings") == 0

    await store.initialize()

    assert await store.create_first_admin(**_ADMIN) is True
    admin = await store.get_user_by_login_name("ADMIN")
    assert (admin["id"], admin["email"], admin["role"]) == ("u-admin", None, "admin")


async def test_bootstrap_keeps_serving_and_schedules_the_retry(pool, monkeypatch) -> None:
    scheduled: list[tuple[object, str]] = []
    monkeypatch.setattr(
        bootstrap,
        "_schedule_deferred_schema_migration",
        lambda ensure_schema, subject: scheduled.append((ensure_schema, subject)),
    )
    store = PostgresOperationalStore(pool)

    async with _held_like_pg_dump(pool):
        await bootstrap._initialize_operational_store(store)  # returns: the store serves

    assert scheduled == [(store.initialize, "operational schema")]
    assert not _ACCOUNT_COLUMNS & await _users_columns(pool)
    assert (await store.get_user_by_email("old@example.com"))["login_name"] is None


async def test_erasure_works_before_the_upgrade(pool) -> None:
    """Soft and hard delete reach the phone codes without a phone column to match."""
    store = PostgresOperationalStore(pool)

    async with _held_like_pg_dump(pool):
        await _start_deferred(store)
        await store.delete_user("u-old", admin_ip="203.0.113.7", admin_id="admin")
        claim = await store.begin_hard_delete_user("u-old")
        counts = await store.hard_delete_user(
            "u-old", claim_token=claim, admin_ip="203.0.113.7", admin_id="admin"
        )

    assert counts["users"] == 1
    assert await store.get_user_by_id("u-old") is None
