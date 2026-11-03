"""The SQL that writes and reads the ``admin_audit_log`` table.

Both the store and the legacy raw-pool path in
``serving.servers.auth.log_admin_action`` insert into it, so they share this
module rather than each carrying a copy that can drift. The table comes from
the schema migrations.

The attribution columns (``actor_type``, ``actor_id``, ``source_ip``,
``target_type``, ``target_id``; see :mod:`serving.audit`) were added after the
table shipped. They are nullable with no default, so adding them rewrites no
row and every older row reads NULL there: actor "not recorded", source "not
recorded" (its raw ``admin_ip`` stays as written), target untyped. Nothing
backfills them.

They are a *required* schema: every audit write names them, and there is no
old-format fallback that would quietly drop an actor the request knew.

``actor_id`` deliberately has no foreign key to ``users``: deleting an account
must not cascade into, or be blocked by, the record of what it did.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from serving.audit import AuditColumns

# The attribution columns, in the order every statement below lists them.
ATTRIBUTION_COLUMNS = ("actor_type", "actor_id", "source_ip", "target_type", "target_id")

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
