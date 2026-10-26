"""The audit attribution columns land before any migration startup may defer, against PostgreSQL.

``admin_audit_log``'s attribution columns (``serving.storage.audit_schema``) are
required: every audit write names them. Ordinary operational migrations are
not: a lock timeout on one (the ``users.role`` default here, behind the nightly
``pg_dump``) lets bootstrap expose the store and retry the rest in the
background. If such a migration runs first, its timeout leaves the store before
the audit columns are installed, and every audit write and read then fails with
``UndefinedColumnError`` until the retry lands. So the store installs them
before any deferrable exit, and refuses to start when it cannot.

Each test builds the current tables in a schema of its own, then takes the
audit attribution columns away again as a database from before them would
have it, so the shared test database's tables are not touched.
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from serving.audit import AuditActor, AuditTarget
from serving.servers import bootstrap
from serving.storage import account_schema, log_schema
from serving.storage.audit_schema import ATTRIBUTION_COLUMNS
from serving.storage.log_schema import column_metadata, existing_columns
from serving.storage.postgres_operational import (
    PostgresOperationalStore,
    RequiredOperationalSchemaUnavailable,
)
from tests.fixtures import postgres

if TYPE_CHECKING:
    import asyncpg

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ADMIN_ID = "u-audit-admin"
_TARGET_ID = "u-audit-target"
_SOURCE_IP = "203.0.113.7"


@pytest.fixture(autouse=True)
def _short_lock_wait(monkeypatch):
    """Time out on the held lock in a fraction of the production bound."""
    monkeypatch.setattr(log_schema, "_DDL_LOCK_TIMEOUT", "300ms")


@pytest.fixture
def scheduled(monkeypatch) -> list[tuple[object, str]]:
    """What bootstrap scheduled as a deferred background migration."""
    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        bootstrap,
        "_schedule_deferred_schema_migration",
        lambda ensure_schema, subject: calls.append((ensure_schema, subject)),
    )
    return calls


@pytest_asyncio.fixture
async def pool(monkeypatch):
    """A pool whose search path is a fresh schema holding the current operational tables."""
    # The store publishes what it reads for SQL outside it; leave the process
    # with the complete schema whatever happens below.
    monkeypatch.setattr(account_schema, "_current", account_schema.COMPLETE)
    admin = await postgres.connect()
    schema = f"audit_order_{secrets.token_hex(4)}"
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    try:
        pool = await postgres.create_pool(
            min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        await PostgresOperationalStore(pool).initialize()
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _make_legacy(pool: asyncpg.Pool) -> None:
    """Drop only the audit attribution columns and stale ``users.role``'s default.

    The audit table is then as it was before attribution, and the role default
    is the first ordinary migration startup reaches whose lock timeout
    propagates: exactly what a held ``users`` lets bootstrap defer.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE admin_audit_log "
            + ", ".join(f"DROP COLUMN {column}" for column in ATTRIBUTION_COLUMNS)
        )
        await conn.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'pro'")
    assert not set(ATTRIBUTION_COLUMNS) & await _audit_columns(pool)


@asynccontextmanager
async def _held_like_pg_dump(pool: asyncpg.Pool, *tables: str):
    """Hold ACCESS SHARE on *tables* from another session, as ``pg_dump`` does all run."""
    reader = await pool.acquire()
    transaction = reader.transaction()
    await transaction.start()
    try:
        await reader.execute(f"LOCK TABLE {', '.join(tables)} IN ACCESS SHARE MODE")
        yield
    finally:
        await transaction.rollback()
        await pool.release(reader)


async def _audit_columns(pool: asyncpg.Pool) -> set[str]:
    async with pool.acquire() as conn:
        return await existing_columns(conn, "admin_audit_log")


async def _role_default(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        meta = await column_metadata(conn, "users", "role")
    assert meta is not None
    return str(meta["default_expr"])


async def test_deferred_startup_installs_audit_columns_first(pool, scheduled) -> None:
    """A users migration may wait for pg_dump; the audit attribution may not."""
    await _make_legacy(pool)
    store = PostgresOperationalStore(pool)

    async with _held_like_pg_dump(pool, "users"):
        await bootstrap._initialize_operational_store(store)  # returns: the store serves

        # The ordinary migration really was the one deferred...
        assert scheduled == [(store.initialize, "operational schema")]
        assert "'pro'" in await _role_default(pool)
        # ...and the required one landed before the store was exposed.
        assert set(ATTRIBUTION_COLUMNS) <= await _audit_columns(pool)

        await store.log_admin_action(
            action="suspend_user",
            details={"reason": "abuse"},
            actor=AuditActor.admin(_ADMIN_ID, _SOURCE_IP),
            target=AuditTarget.user(_TARGET_ID),
        )
        total, [row] = await store.list_audit_log(target_user_id=_TARGET_ID)

    assert total == 1
    assert row["action"] == "suspend_user"
    assert (row["actor_type"], row["actor_id"], row["source_ip"]) == (
        "admin",
        _ADMIN_ID,
        _SOURCE_IP,
    )
    assert row["admin_ip"] == _SOURCE_IP
    assert (row["target_type"], row["target_id"], row["target_user_id"]) == (
        "user",
        _TARGET_ID,
        _TARGET_ID,
    )

    # The backup ended: bootstrap's background retry lands the rest.
    await store.initialize()
    assert "'free'" in await _role_default(pool)


async def test_locked_audit_table_refuses_startup_instead_of_deferring(pool, scheduled) -> None:
    """Behind a pg_dump holding the audit table too, startup fails rather than serves."""
    await _make_legacy(pool)
    store = PostgresOperationalStore(pool)

    async with _held_like_pg_dump(pool, "users", "admin_audit_log"):
        with pytest.raises(RequiredOperationalSchemaUnavailable, match="admin_audit_log"):
            await bootstrap._initialize_operational_store(store)

    assert scheduled == []
    assert not set(ATTRIBUTION_COLUMNS) & await _audit_columns(pool)


async def test_complete_audit_schema_starts_behind_pg_dump(pool, scheduled) -> None:
    """A settled audit table asks for no ALTER TABLE lock, so a held one costs nothing."""
    store = PostgresOperationalStore(pool)

    async with _held_like_pg_dump(pool, "users", "admin_audit_log"):
        await bootstrap._initialize_operational_store(store)

    assert scheduled == []
    assert set(ATTRIBUTION_COLUMNS) <= await _audit_columns(pool)
