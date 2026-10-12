"""Users schema for email-less accounts, in both boot-time builders.

``DatabaseLogger._create_tables`` (bootstrap) and
``PostgresOperationalStore.initialize`` each carry a copy of the users DDL and
its migrations; a fresh database and an upgraded one must both end up with a
nullable ``email`` and a case-insensitively unique ``login_name``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.config.settings import get_settings
from serving.storage.database import DatabaseLogger
from serving.storage.log_schema import SchemaLockUnavailable
from serving.storage.postgres_operational import (
    PostgresOperationalStore,
    RequiredOperationalSchemaUnavailable,
)

_EMAIL_DROP_NOT_NULL = "ALTER TABLE users ALTER COLUMN email DROP NOT NULL"
_LOGIN_NAME_ADD = "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_name TEXT"


class _LockTimeout(Exception):
    """What asyncpg raises when ``lock_timeout`` expires."""

    sqlstate = "55P03"


def _recording_pool(
    *, email_not_null: bool, locked: str | None = None
) -> tuple[MagicMock, list[tuple[str, tuple]]]:
    """A pool whose connection records statements and simulates the catalog.

    The column catalog is empty (every ``ADD COLUMN`` migration runs) and the
    only column with metadata is ``users.email``, NOT NULL or not. The
    statement *locked* times out on its lock, as it would behind ``pg_dump``.
    """
    statements: list[tuple[str, tuple]] = []
    conn = MagicMock()

    async def _execute(sql: str, *args: Any) -> str:
        statements.append((sql, args))
        if locked is not None and " ".join(sql.split()) == locked:
            raise _LockTimeout(f"canceling statement due to lock timeout: {locked}")
        return "UPDATE 0"

    async def _fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        if args == ("users", "email"):
            return {"not_null": email_not_null, "default_expr": None}
        return None

    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm
    return pool, statements


async def _run_builder(builder: str, pool: MagicMock) -> None:
    if builder == "database":
        db = DatabaseLogger({"dsn": "postgres://ignored"})
        db.pool = pool
        await db._create_tables()
    else:
        await PostgresOperationalStore(pool).initialize()


def _sql(statements: list[tuple[str, tuple]]) -> list[str]:
    return [" ".join(sql.split()) for sql, _ in statements]


BUILDERS = ["database", "operational"]


@pytest.mark.parametrize("builder", BUILDERS)
async def test_fresh_users_table_admits_email_less_accounts(builder):
    pool, statements = _recording_pool(email_not_null=False)
    await _run_builder(builder, pool)

    create = next(s for s in _sql(statements) if "CREATE TABLE IF NOT EXISTS users" in s)
    assert "email TEXT UNIQUE," in create
    assert "email TEXT NOT NULL" not in create
    assert "login_name TEXT," in create
    assert _EMAIL_DROP_NOT_NULL not in _sql(statements)


@pytest.mark.parametrize("builder", BUILDERS)
async def test_upgrade_adds_login_name_then_indexes_it(builder):
    pool, statements = _recording_pool(email_not_null=True)
    await _run_builder(builder, pool)
    sql = _sql(statements)

    add_column = sql.index("ALTER TABLE users ADD COLUMN IF NOT EXISTS login_name TEXT")
    index = next(i for i, s in enumerate(sql) if "idx_users_login_name" in s)
    assert add_column < index, "the index would reference a column that does not exist yet"
    assert sql[index] == (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_name "
        "ON users (lower(login_name)) WHERE login_name IS NOT NULL"
    )


@pytest.mark.parametrize("builder", BUILDERS)
async def test_email_not_null_is_dropped_under_the_lock_guard(builder):
    pool, statements = _recording_pool(email_not_null=True)
    await _run_builder(builder, pool)
    sql = _sql(statements)

    drop = sql.index(_EMAIL_DROP_NOT_NULL)
    # bounded_ddl brackets the strong-lock statement with a lock_timeout.
    assert sql[drop - 1].startswith("SET lock_timeout = '")
    assert sql[drop + 1] == "SET lock_timeout = DEFAULT"


@pytest.mark.parametrize("builder", BUILDERS)
async def test_admin_email_seeding_reads_settings(builder, monkeypatch):
    # Settings, not os.environ: a database-backed value must apply too.
    monkeypatch.setattr(get_settings(), "admin_emails", " Root@Example.com, ops@example.com ,")
    pool, statements = _recording_pool(email_not_null=False)
    await _run_builder(builder, pool)

    seeding = [args for sql, args in statements if "SET role = 'admin'" in " ".join(sql.split())]
    assert seeding == [(["root@example.com", "ops@example.com"],)]


@pytest.mark.parametrize("builder", BUILDERS)
async def test_no_admin_emails_no_seeding(builder, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_emails", "")
    pool, statements = _recording_pool(email_not_null=False)
    await _run_builder(builder, pool)

    assert not any("SET role = 'admin'" in " ".join(sql.split()) for sql, _ in statements)


@pytest.mark.parametrize("locked", [_LOGIN_NAME_ADD, _EMAIL_DROP_NOT_NULL])
async def test_the_operational_store_refuses_to_serve_without_the_sign_in_schema(locked):
    """Every account lookup selects login_name: a deferred migration would break sign-in."""
    pool, _statements = _recording_pool(email_not_null=True, locked=locked)

    with pytest.raises(RequiredOperationalSchemaUnavailable):
        await PostgresOperationalStore(pool).initialize()


async def test_a_deferred_users_migration_cannot_strand_login_name():
    """A later, deferrable column may wait; the sign-in columns land before it."""
    pool, statements = _recording_pool(
        email_not_null=True,
        locked="ALTER TABLE users ADD COLUMN IF NOT EXISTS approval_note TEXT",
    )

    with pytest.raises(SchemaLockUnavailable) as excinfo:
        await PostgresOperationalStore(pool).initialize()

    # Deferred, not refused: bootstrap retries it in the background.
    assert not isinstance(excinfo.value, RequiredOperationalSchemaUnavailable)
    sql = _sql(statements)
    assert _LOGIN_NAME_ADD in sql
    assert _EMAIL_DROP_NOT_NULL in sql
    assert any("idx_users_login_name" in statement for statement in sql)
