"""Lock-safety guards for the shared ``api_logs`` schema initializer.

These pin the behaviour that a production outage turned up: a deploy adding a
column to ``api_logs`` while the nightly ``pg_dump`` held the table queued an
``ACCESS EXCLUSIVE`` request, waited out asyncpg's 60 s default three times, and
left the gateway running with database logging disabled — permanently unhealthy,
which then stopped compose from starting the frontend and 502'd the whole site.

The invariants below are what keep that from recurring:

1. DDL runs under a bounded ``lock_timeout`` so a contended migration fails
   immediately instead of parking every reader behind it.
2. The guard is always lifted, including on failure — the connection returns to
   a pool and must not carry the timeout into ordinary queries.
3. A lock timeout (SQLSTATE 55P03) is reported as :class:`SchemaLockUnavailable`
   so callers can tell "someone else holds the table" from "the schema is
   broken"; anything else propagates untouched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.log_schema import (
    _DDL_LOCK_TIMEOUT,
    SchemaLockUnavailable,
    ensure_api_logs_schema,
)


class _PostgresError(Exception):
    """Stand-in for an asyncpg error, which carries ``sqlstate``."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"postgres error {sqlstate}")
        self.sqlstate = sqlstate


def _conn(execute_side_effect=None) -> tuple[MagicMock, list[str]]:
    """Return ``(conn, statements)``; empty catalogs so every migration runs."""
    statements: list[str] = []
    conn = MagicMock()

    async def _execute(sql: str, *_args: object) -> str:
        statements.append(sql)
        if execute_side_effect is not None:
            execute_side_effect(sql)
        return "OK"

    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetch = AsyncMock(return_value=[])
    return conn, statements


@pytest.mark.asyncio
async def test_ddl_runs_under_a_bounded_lock_timeout():
    """Migrations are bracketed by a SET/reset pair, not issued bare."""
    conn, statements = _conn()

    await ensure_api_logs_schema(conn)

    assert f"SET lock_timeout = '{_DDL_LOCK_TIMEOUT}'" in statements
    assert "SET lock_timeout = DEFAULT" in statements

    set_at = statements.index(f"SET lock_timeout = '{_DDL_LOCK_TIMEOUT}'")
    reset_at = statements.index("SET lock_timeout = DEFAULT")
    altered = [i for i, s in enumerate(statements) if s.strip().startswith("ALTER TABLE")]
    indexed = [i for i, s in enumerate(statements) if s.strip().startswith("CREATE INDEX")]
    assert altered, "expected the fresh-install path to emit column migrations"
    assert indexed, "expected the fresh-install path to emit index DDL"
    for position in altered + indexed:
        assert set_at < position < reset_at, (
            "every lock-taking statement must sit inside the bounded-wait window"
        )


@pytest.mark.asyncio
async def test_lock_timeout_is_reset_even_when_ddl_fails():
    """A failed migration must not leave lock_timeout set on a pooled conn."""

    def _boom(sql: str) -> None:
        if sql.strip().startswith("ALTER TABLE"):
            raise _PostgresError("55P03")

    conn, statements = _conn(execute_side_effect=_boom)

    with pytest.raises(SchemaLockUnavailable):
        await ensure_api_logs_schema(conn)

    assert statements[-1] == "SET lock_timeout = DEFAULT", (
        "the guard must be lifted on the failure path too, or every later query "
        "on this pooled connection inherits the DDL timeout"
    )


@pytest.mark.asyncio
async def test_lock_timeout_surfaces_as_schema_lock_unavailable():
    """55P03 is a transient lock conflict, not a broken schema."""

    def _boom(sql: str) -> None:
        if sql.strip().startswith("ALTER TABLE"):
            raise _PostgresError("55P03")

    conn, _ = _conn(execute_side_effect=_boom)

    with pytest.raises(SchemaLockUnavailable) as excinfo:
        await ensure_api_logs_schema(conn)

    assert "api_logs" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, _PostgresError)


@pytest.mark.asyncio
async def test_non_lock_errors_are_not_reclassified():
    """A real DDL error must keep its type, so callers still treat it as fatal."""

    def _boom(sql: str) -> None:
        if sql.strip().startswith("ALTER TABLE"):
            raise _PostgresError("42703")  # undefined_column

    conn, _ = _conn(execute_side_effect=_boom)

    with pytest.raises(_PostgresError):
        await ensure_api_logs_schema(conn)


@pytest.mark.asyncio
async def test_steady_state_startup_takes_no_lock_and_no_guard_leak():
    """With the schema current, the guard opens and closes around no DDL."""
    from serving.storage.log_schema import _API_LOGS_COLUMN_MIGRATIONS, _API_LOGS_INDEXES

    conn, statements = _conn()
    conn.fetch = AsyncMock(
        side_effect=[
            [{"attname": name} for name, _ in _API_LOGS_COLUMN_MIGRATIONS],
            [{"indexname": name} for name, _ in _API_LOGS_INDEXES],
        ]
    )

    await ensure_api_logs_schema(conn)

    assert statements.count("SET lock_timeout = DEFAULT") == 1
    lock_taking = [
        s for s in statements if s.strip().startswith(("ALTER TABLE", "CREATE INDEX", "DROP INDEX"))
    ]
    assert lock_taking == []
