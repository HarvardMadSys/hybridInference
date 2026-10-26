"""PostgresOperationalStore: the audit rows its own transactions write.

Bootstrap, soft delete, resume and hard delete insert their audit row inside
the transaction that makes the change, so the route cannot write it; they
take the actor instead. Mocked connections, no database:
``tests/integration/storage/test_audit_attribution_postgres.py`` runs the same
writers against PostgreSQL.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from serving.audit import AuditActor
from serving.storage.audit_schema import (
    ATTRIBUTION_COLUMNS,
    COLUMN_MIGRATIONS,
    INSERT_SQL,
    ensure_admin_audit_log_schema,
)
from serving.storage.log_schema import SchemaLockUnavailable
from serving.storage.postgres_operational import PostgresOperationalStore

_COLUMNS = ("admin_ip", "action", "target_user_id", "details", "success", *ATTRIBUTION_COLUMNS)


class _Conn:
    """Records each statement, and whether it ran inside the transaction."""

    def __init__(self, fetchrow: dict[str, Any] | None = None) -> None:
        self.statements: list[tuple[str, tuple[Any, ...], bool]] = []
        self._fetchrow = fetchrow
        self._depth = 0

    @asynccontextmanager
    async def transaction(self):
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append((sql, args, self._depth > 0))
        return "DELETE 1"

    async def fetchrow(self, sql: str, *args: Any):
        return self._fetchrow

    async def fetchval(self, sql: str, *args: Any):
        return None

    async def fetch(self, sql: str, *args: Any):
        # The catalog read for users' columns: no phone column, so the phone
        # cleanup takes its simple branch.
        return []

    def audit_rows(self) -> list[tuple[dict[str, Any], bool]]:
        rows = []
        for sql, args, in_tx in self.statements:
            if sql == INSERT_SQL:
                row = dict(zip(_COLUMNS, args, strict=True))
                row["details"] = json.loads(row["details"]) if row["details"] else None
                rows.append((row, in_tx))
        return rows


def _store(conn: _Conn) -> PostgresOperationalStore:
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return PostgresOperationalStore(pool)


ADMIN = AuditActor.admin("u-admin", "192.0.2.10")


async def test_admin_soft_delete_audits_the_actor_in_its_transaction():
    """delete_user used to receive the admin and drop it from its row."""
    conn = _Conn()

    await _store(conn).delete_user(
        "u-target", admin_id="ops@example.test", reason="spam", email="t@example.test", actor=ADMIN
    )

    [(row, in_tx)] = conn.audit_rows()
    assert in_tx, "the audit row must commit or roll back with the deletion"
    assert row["action"] == "delete_user"
    assert (row["actor_type"], row["actor_id"], row["source_ip"]) == (
        "admin",
        "u-admin",
        "192.0.2.10",
    )
    assert (row["target_type"], row["target_id"], row["target_user_id"]) == (
        "user",
        "u-target",
        "u-target",
    )
    # The display identity is kept as context, like resume and hard delete.
    assert row["details"] == {
        "email": "t@example.test",
        "reason": "spam",
        "admin_id": "ops@example.test",
    }


async def test_self_service_delete_is_the_account_acting_on_itself():
    conn = _Conn(fetchrow={"status": "active", "role": "free"})

    await _store(conn).delete_user(
        "u-owner",
        reason="Deleted by the account owner",
        self_service=True,
        actor=AuditActor.user("u-owner", "192.0.2.11"),
    )

    [(row, in_tx)] = conn.audit_rows()
    assert in_tx
    assert (row["actor_type"], row["actor_id"]) == ("user", "u-owner")
    assert row["target_user_id"] == "u-owner"
    assert row["details"]["self_service"] is True
    # No administrator was involved, so none is named.
    assert "admin_id" not in row["details"]


async def test_a_legacy_delete_call_still_writes_a_row_with_no_actor():
    conn = _Conn()

    await _store(conn).delete_user("u-target", admin_ip="ops@example.test", admin_id="ops")

    [(row, _in_tx)] = conn.audit_rows()
    assert row["admin_ip"] == "ops@example.test"
    assert row["actor_type"] is row["actor_id"] is row["source_ip"] is None
    # The method itself knows its target is the account it deletes.
    assert (row["target_type"], row["target_user_id"]) == ("user", "u-target")


async def test_resume_audits_the_actor_in_its_transaction():
    conn = _Conn(fetchrow={"status": "deleted", "hard_delete_pending": False})

    await _store(conn).resume_user("u-target", admin_id="ops", reason="appeal", actor=ADMIN)

    [(row, in_tx)] = conn.audit_rows()
    assert in_tx
    assert row["action"] == "resume_user"
    assert row["actor_id"] == "u-admin"
    assert row["target_user_id"] == "u-target"


async def test_hard_delete_erases_rows_about_the_user_but_not_rows_by_actors():
    """Erasure reaches the user's own rows (``target_user_id``) and no actor column."""
    conn = _Conn(
        fetchrow={"status": "deleted", "hard_delete_pending": True, "hard_delete_claim_token": "c"}
    )

    await _store(conn).hard_delete_user("u-target", claim_token="c", admin_id="ops", actor=ADMIN)

    audit_deletes = [sql for sql, _args, _tx in conn.statements if "admin_audit_log" in sql]
    erasures = [sql for sql in audit_deletes if sql.startswith("DELETE")]
    assert erasures == ["DELETE FROM admin_audit_log WHERE target_user_id = $1"]
    [(row, in_tx)] = conn.audit_rows()
    assert in_tx
    assert row["action"] == "hard_delete_user"
    assert (row["actor_type"], row["actor_id"]) == ("admin", "u-admin")
    assert row["target_user_id"] == "u-target"


async def test_first_run_setup_is_attributed_to_bootstrap_not_an_account():
    conn = _Conn()

    created = await _store(conn).create_first_admin(
        user_id="u-first",
        login_name="Admin",
        password_hash="argon2-hash",
        user_name="Ops",
        marker_key="setup_completed_at",
        marker_value="2026-10-25T00:00:00+00:00",
        code_key="setup_code",
        actor=AuditActor.bootstrap("192.0.2.12"),
    )

    assert created is True
    [(row, in_tx)] = conn.audit_rows()
    assert in_tx
    assert row["action"] == "setup.admin_created"
    # Nobody had an account yet; the new administrator is the target, not the actor.
    assert (row["actor_type"], row["actor_id"], row["source_ip"]) == (
        "bootstrap",
        None,
        "192.0.2.12",
    )
    assert (row["target_type"], row["target_user_id"]) == ("user", "u-first")


async def test_the_audit_list_selects_the_attribution_columns():
    conn = _Conn(fetchrow={"total": 0})
    calls: list[str] = []

    async def _fetch(sql: str, *args: Any):
        calls.append(sql)
        return []

    conn.fetch = _fetch  # type: ignore[method-assign]
    await _store(conn).list_audit_log(target_user_id="u-1")

    [select] = calls
    for column in ATTRIBUTION_COLUMNS:
        assert column in select
    assert "target_user_id = $1" in select


class _CatalogConn:
    """A connection whose catalog already lists *present* audit columns."""

    def __init__(self, present: set[str], *, lock_timeout: bool = False) -> None:
        self.present = present
        self.lock_timeout = lock_timeout
        self.executed: list[str] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql)
        if self.lock_timeout and sql.startswith("ALTER TABLE"):
            error = RuntimeError("canceling statement due to lock timeout")
            error.sqlstate = "55P03"  # type: ignore[attr-defined]
            raise error
        return "OK"

    async def fetch(self, sql: str, *args: Any):
        return [{"attname": name} for name in self.present]


_LEGACY = {"id", "timestamp", "admin_ip", "action", "target_user_id", "details", "success"}


async def test_a_settled_table_takes_no_ddl_lock():
    """Every boot after the upgrade reads the catalog and issues no ALTER at all."""
    conn = _CatalogConn(_LEGACY | set(ATTRIBUTION_COLUMNS))

    await ensure_admin_audit_log_schema(conn)  # type: ignore[arg-type]

    assert not any(sql.startswith("ALTER") for sql in conn.executed)
    assert not any("lock_timeout" in sql for sql in conn.executed)


async def test_a_legacy_table_gets_only_additive_columns_under_a_bounded_wait():
    conn = _CatalogConn(set(_LEGACY))

    await ensure_admin_audit_log_schema(conn)  # type: ignore[arg-type]

    alters = [sql for sql in conn.executed if sql.startswith("ALTER")]
    assert alters == [ddl for _name, ddl in COLUMN_MIGRATIONS]
    # Nullable, no default: no row is rewritten and old rows read NULL.
    assert all(ddl.endswith(" TEXT") for ddl in alters)
    assert not any(sql.startswith("UPDATE") for sql in conn.executed)
    assert any("lock_timeout" in sql for sql in conn.executed)


async def test_a_held_table_raises_instead_of_waiting():
    conn = _CatalogConn(set(_LEGACY), lock_timeout=True)

    with pytest.raises(SchemaLockUnavailable):
        await ensure_admin_audit_log_schema(conn)  # type: ignore[arg-type]
