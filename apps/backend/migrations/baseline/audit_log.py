"""The ``admin_audit_log`` table and its attribution columns, frozen.

Copied from ``serving.storage.audit_schema``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from migrations.baseline.ddl import apply_column_migrations

if TYPE_CHECKING:
    import asyncpg


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
