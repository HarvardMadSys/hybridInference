"""The guarded-DDL helpers the startup builders used, frozen with them.

Catalog first, strong-lock DDL only when something is actually missing, and
always under a bounded lock wait that surfaces contention as
:class:`SchemaLockUnavailable`. Copied from ``serving.storage.log_schema``.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    import asyncpg


# How long a migration waits for its table lock before Postgres cancels it.
#
# The catalog pre-check below removes the lock from the *steady-state* path, but
# not from the deploy that actually adds a column — that one has to take
# ``ACCESS EXCLUSIVE``. Waiting for it is the dangerous part: a queued exclusive
# request also parks every read that arrives behind it, so a migration blocked
# by one long-running reader stalls the whole table for as long as it waits.
# The readers that block it are long: the nightly ``pg_dump`` holds
# ``ACCESS SHARE`` over ``api_logs`` for hours. Bounded low so a contended
# migration fails immediately and leaves the queue clear; the caller retries it
# later rather than holding startup (or the table) hostage.
_DDL_LOCK_TIMEOUT = "3s"

# SQLSTATE Postgres raises when ``lock_timeout`` expires (asyncpg surfaces it as
# ``LockNotAvailableError``). Matched by code rather than by exception class so
# this module keeps its asyncpg import type-only.
_LOCK_NOT_AVAILABLE = "55P03"


class SchemaLockUnavailable(RuntimeError):
    """A migration could not acquire its table lock within the timeout.

    Distinct from a genuine schema error: the connection is healthy and the
    statement is valid, another session simply holds a conflicting lock. Callers
    should treat it as transient and retry, not as a reason to give up on the
    database.
    """


@asynccontextmanager
async def _bounded_lock_wait(conn: asyncpg.Connection) -> AsyncIterator[None]:
    """Bound how long DDL inside the block waits on a lock.

    Reset on the way out because the connection goes back to a pool: leaving
    ``lock_timeout`` set would silently apply it to ordinary query traffic.
    """
    await conn.execute(f"SET lock_timeout = '{_DDL_LOCK_TIMEOUT}'")
    try:
        yield
    finally:
        await conn.execute("SET lock_timeout = DEFAULT")


async def _execute_ddl(conn: asyncpg.Connection, ddl: str) -> None:
    """Run one DDL statement, translating a lock timeout into a typed error."""
    try:
        await conn.execute(ddl)
    except Exception as exc:
        if getattr(exc, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
            raise
        raise SchemaLockUnavailable(
            f"could not acquire the lock for {ddl!r} within {_DDL_LOCK_TIMEOUT}; "
            "another session holds a conflicting lock"
        ) from exc


# Public faces of the guard pair, for the other schema builders
# (``database.py`` / ``postgres_operational.py``) that adopt the same
# doctrine: catalog first, strong-lock DDL only when actually needed, and
# always under the bounded wait. The 2026-08-21 production outage was this
# module's api_logs incident replayed on ``api_keys`` — the boot-time
# builders still issued their idempotent ALTERs bare, and the first restart
# inside the nightly ``pg_dump`` window queued behind it for the pool's
# 60 s command timeout, three times, and failed the deploy.
bounded_ddl = _bounded_lock_wait
execute_ddl = _execute_ddl


async def existing_columns(conn: asyncpg.Connection, table: str) -> set[str]:
    """Column names of *table*; empty when the table does not exist."""
    rows = await conn.fetch(
        """
        SELECT attname
        FROM pg_attribute
        WHERE attrelid = to_regclass($1)
          AND NOT attisdropped
          AND attnum > 0
        """,
        table,
    )
    return {r["attname"] for r in rows}


async def apply_column_migrations(
    conn: asyncpg.Connection,
    table: str,
    migrations: Sequence[tuple[str, str]],
) -> None:
    """Issue only the ``ADD COLUMN`` DDL whose column is actually missing.

    The steady-state startup (every column already present) reads the catalog
    once and issues no DDL at all — no ``ACCESS EXCLUSIVE`` request, nothing
    for a long-running reader like the nightly ``pg_dump`` to block. DDL that
    does run sits under the bounded lock wait and surfaces contention as
    :class:`SchemaLockUnavailable`. The ``IF NOT EXISTS`` clause stays on the
    statements as the race guard for two workers booting at once.
    """
    present = await existing_columns(conn, table)
    pending = [ddl for name, ddl in migrations if name not in present]
    if not pending:
        return
    async with _bounded_lock_wait(conn):
        for ddl in pending:
            await _execute_ddl(conn, ddl)


async def drop_columns_if_present(
    conn: asyncpg.Connection,
    table: str,
    drops: Sequence[tuple[str, str]],
) -> None:
    """Issue only the ``DROP COLUMN`` DDL whose column still exists."""
    present = await existing_columns(conn, table)
    pending = [ddl for name, ddl in drops if name in present]
    if not pending:
        return
    async with _bounded_lock_wait(conn):
        for ddl in pending:
            await _execute_ddl(conn, ddl)


async def constraint_definition(conn: asyncpg.Connection, table: str, name: str) -> str | None:
    """``pg_get_constraintdef`` of *name* on *table*, or None when absent.

    Lets a constraint rebuild be gated on what the constraint currently says
    (e.g. skip when the CHECK already admits every required member) instead of
    unconditionally dropping and re-adding it — which takes ``ACCESS
    EXCLUSIVE`` on every single startup.
    """
    return await conn.fetchval(
        """
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass($1)
          AND conname = $2
        """,
        table,
        name,
    )


def constraint_admitted_values(definition: str | None) -> set[str] | None:
    """The quoted string literals a constraint definition admits, or None.

    ``pg_get_constraintdef`` renders a CHECK's members as quoted literals
    (``role = ANY (ARRAY['free'::text, ...])``), so the set of quoted strings
    is the constraint's admitted vocabulary. A gate that skips a rebuild must
    compare this set **exactly**, never by membership: an older, wider
    constraint can admit every current member plus a legacy one —
    ``users_role_check`` once shipped as ``('trial', 'free', 'pro',
    'internal', 'admin')`` — and a subset test judges it settled, silently
    skipping both the rebuild and the row migration the rebuild carries.
    """
    if definition is None:
        return None
    return set(re.findall(r"'([^']*)'", definition))


async def column_metadata(
    conn: asyncpg.Connection, table: str, column: str
) -> dict[str, object] | None:
    """``{"not_null": bool, "default_expr": str | None}`` for one column.

    None when the table or column does not exist. Gates ``ALTER COLUMN ...
    SET DEFAULT / SET NOT NULL`` statements the same way the column
    migrations are gated: read the catalog, only lock when the change is
    actually needed.
    """
    row = await conn.fetchrow(
        """
        SELECT a.attnotnull AS not_null,
               pg_get_expr(d.adbin, d.adrelid) AS default_expr
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d
          ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = to_regclass($1)
          AND a.attname = $2
          AND NOT a.attisdropped
        """,
        table,
        column,
    )
    return dict(row) if row is not None else None


def parse_command_tag_count(command_tag: str) -> int:
    """Extract the affected row count from an asyncpg command tag."""
    parts = command_tag.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0
