"""The ``admin_audit_log`` table: its schema, and the SQL that writes and reads it.

Both boot-time schema builders (``DatabaseLogger`` and
``PostgresOperationalStore``) create the table, and both the store and the
legacy raw-pool path in ``serving.servers.auth.log_admin_action`` insert into
it, so they share this module rather than each carrying a copy that can drift.

The attribution columns (``actor_type``, ``actor_id``, ``source_ip``,
``target_type``, ``target_id``; see :mod:`serving.audit`) were added after the
table shipped. They are nullable with no default, so adding them rewrites no
row and every older row reads NULL there: actor "not recorded", source "not
recorded" (its raw ``admin_ip`` stays as written), target untyped. Nothing
backfills them.

They are a *required* schema: every audit write names them, and there is no
old-format fallback that would quietly drop an actor the request knew. The
migration is catalog-gated (a settled database issues no DDL and takes no
table lock) and runs under the bounded lock wait
(:mod:`serving.storage.log_schema`). ``PostgresOperationalStore`` turns a lock
timeout into ``RequiredOperationalSchemaUnavailable``, which stops startup
rather than serving against a table that cannot hold attribution. The
``DatabaseLogger`` pass runs first on the same database and defers its timeout
as usual; the operational store's pass right after it is the gate.

``actor_id`` deliberately has no foreign key to ``users``: deleting an account
must not cascade into, or be blocked by, the record of what it did.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from serving.storage.log_schema import apply_column_migrations

if TYPE_CHECKING:
    import asyncpg

    from serving.audit import AuditColumns

CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS admin_audit_log (
        id BIGSERIAL PRIMARY KEY,
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        admin_ip TEXT NOT NULL,
        action TEXT NOT NULL,
        target_user_id TEXT,
        details JSONB,
        success BOOLEAN DEFAULT TRUE,
        actor_type TEXT,
        actor_id TEXT,
        source_ip TEXT,
        target_type TEXT,
        target_id TEXT
    )
"""

CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_timestamp ON admin_audit_log(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_user "
    "ON admin_audit_log(target_user_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action, timestamp DESC)",
)

# The attribution columns, in the order every statement below lists them.
ATTRIBUTION_COLUMNS = ("actor_type", "actor_id", "source_ip", "target_type", "target_id")

COLUMN_MIGRATIONS = tuple(
    (column, f"ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS {column} TEXT")
    for column in ATTRIBUTION_COLUMNS
)

# The legacy columns keep their parameter positions ($1-$5).
INSERT_SQL = (
    "INSERT INTO admin_audit_log "
    "(admin_ip, action, target_user_id, details, success, "
    f"{', '.join(ATTRIBUTION_COLUMNS)}) "
    "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10)"
)

SELECT_COLUMNS = "id, timestamp, admin_ip, action, target_user_id, details, success, " + ", ".join(
    ATTRIBUTION_COLUMNS
)


async def ensure_admin_audit_log_schema(conn: asyncpg.Connection) -> None:
    """Create ``admin_audit_log`` and its indexes, then add any missing attribution column.

    Raises:
        SchemaLockUnavailable: Another session holds the table (usually
            ``pg_dump``); no column was added.
    """
    await conn.execute(CREATE_TABLE_SQL)
    for statement in CREATE_INDEX_SQL:
        await conn.execute(statement)
    await apply_column_migrations(conn, "admin_audit_log", COLUMN_MIGRATIONS)


async def insert_audit_row(
    conn: asyncpg.Connection,
    *,
    columns: AuditColumns,
    action: str,
    details: dict[str, Any] | None,
    success: bool,
) -> None:
    """Insert one audit row on *conn*, inside whatever transaction it is in."""
    await conn.execute(
        INSERT_SQL,
        columns.admin_ip,
        action,
        columns.target_user_id,
        json.dumps(details) if details else None,
        success,
        columns.actor_type,
        columns.actor_id,
        columns.source_ip,
        columns.target_type,
        columns.target_id,
    )
