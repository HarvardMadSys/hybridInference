"""Audit actors and a recording audit store for tests.

A test that overrides ``verify_admin_access`` bypasses the code that records
who was admitted, so an audited route's ``get_admin_audit_actor`` has nothing
to read and answers 500 rather than invent an actor. Such a test overrides
``get_admin_audit_actor`` with one of these instead (or as well).

:class:`AuditRowRecorder` runs the real ``PostgresOperationalStore`` audit
writer over a connection that keeps each ``admin_audit_log`` INSERT as a
``{column: value}`` dict, so a test sees the row a request would store rather
than the arguments a route happened to pass.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from serving.audit import AuditActor
from serving.storage.audit_schema import ATTRIBUTION_COLUMNS, INSERT_SQL
from serving.storage.postgres_operational import PostgresOperationalStore

# Documentation addresses (RFC 5737), never a real client.
ADMIN_ACTOR = AuditActor.admin("admin-1", "203.0.113.10")
TOKEN_ACTOR = AuditActor.admin_token("203.0.113.11")

# The columns INSERT_SQL binds, in parameter order.
INSERT_COLUMNS = (
    "admin_ip",
    "action",
    "target_user_id",
    "details",
    "success",
    *ATTRIBUTION_COLUMNS,
)


class AuditRowRecorder:
    """The real store audit writer, over a connection that records its rows."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        conn = MagicMock()

        async def _execute(sql: str, *args: Any) -> str:
            if sql == INSERT_SQL:
                row = dict(zip(INSERT_COLUMNS, args, strict=True))
                if isinstance(row["details"], str):
                    row["details"] = json.loads(row["details"])
                self.rows.append(row)
            return "INSERT 0 1"

        conn.execute = AsyncMock(side_effect=_execute)

        @asynccontextmanager
        async def _acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = _acquire
        self.store = PostgresOperationalStore(pool)

    async def log_admin_action(self, **kwargs: Any) -> None:
        """Write one row through ``PostgresOperationalStore.log_admin_action``."""
        await self.store.log_admin_action(**kwargs)
