"""Integration tests for DualWriteOperationalStore against real SQLite.

Uses two independent in-memory SQLite databases (via the _SqliteD1Client shim)
to verify that writes land in both the primary and shadow stores.  Reads from
each store independently after each operation to confirm data fidelity.

No network, no D1 credentials, no PostgreSQL — pure in-process SQLite.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from serving.observability.tracked_tasks import _TRACKED_TASKS
from serving.storage.dual_write import DualWriteLogStore, DualWriteOperationalStore


async def _drain_shadow_tasks() -> None:
    """Await any pending tracked_task shadow writes scheduled by dual_write."""
    if _TRACKED_TASKS:
        await asyncio.gather(*list(_TRACKED_TASKS), return_exceptions=True)


@pytest.fixture(autouse=True)
def _clear_tracked_tasks_between_tests():
    """Ensure the module-level tracked-task set is empty for each test."""
    _TRACKED_TASKS.clear()
    yield
    _TRACKED_TASKS.clear()

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "apps"
    / "backend"
    / "serving"
    / "storage"
    / "d1_schema.sql"
)


# ---------------------------------------------------------------------------
# SQLite shim (copied from test_d1_sqlite_integration.py)
# ---------------------------------------------------------------------------


class _SqliteD1Client:
    """In-process SQLite that speaks the same interface as D1Client."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    async def query(self, sql: str, params: list[Any] | None = None) -> Any:
        """Execute a single statement, return D1Result-compatible object."""
        from serving.storage.d1_client import D1Result

        cursor = self._conn.execute(sql, params or [])
        if cursor.description:
            columns = [d[0] for d in cursor.description]
            rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
        else:
            rows = []
        self._conn.commit()
        return D1Result(
            rows=rows,
            changes=cursor.rowcount if cursor.rowcount >= 0 else 0,
            last_row_id=cursor.lastrowid or 0,
        )

    async def execute(self, sql: str, params: list[Any] | None = None) -> Any:
        """Alias for query."""
        return await self.query(sql, params)

    async def batch(self, statements: list[tuple[str, list[Any] | None]]) -> list[Any]:
        """Execute statements in a transaction."""
        from serving.storage.d1_client import D1Result

        results = []
        try:
            self._conn.execute("BEGIN")
            for sql, params in statements:
                cursor = self._conn.execute(sql, params or [])
                if cursor.description:
                    columns = [d[0] for d in cursor.description]
                    rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
                else:
                    rows = []
                results.append(
                    D1Result(
                        rows=rows,
                        changes=cursor.rowcount if cursor.rowcount >= 0 else 0,
                        last_row_id=cursor.lastrowid or 0,
                    )
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return results

    async def health_check(self) -> bool:
        """Always healthy."""
        return True

    async def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()


def _load_schema(client: _SqliteD1Client) -> None:
    """Load the D1 schema DDL into the SQLite client."""
    schema_sql = SCHEMA_PATH.read_text()
    for segment in schema_sql.split(";"):
        lines = [ln for ln in segment.splitlines() if not ln.strip().startswith("--")]
        cleaned = "\n".join(lines).strip()
        if cleaned:
            client._conn.execute(cleaned)
    client._conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def primary_client() -> _SqliteD1Client:
    """Primary (D1) SQLite backend."""
    client = _SqliteD1Client()
    _load_schema(client)
    return client


@pytest.fixture
def shadow_client() -> _SqliteD1Client:
    """Shadow (Postgres stand-in) SQLite backend."""
    client = _SqliteD1Client()
    _load_schema(client)
    return client


@pytest.fixture
def stores(primary_client, shadow_client):
    """Create primary, shadow, and dual-write operational stores."""
    from serving.storage.d1_operational import D1OperationalStore

    primary_store = D1OperationalStore(primary_client)
    shadow_store = D1OperationalStore(shadow_client)
    dual_store = DualWriteOperationalStore(primary_store, shadow_store)
    return dual_store, primary_store, shadow_store


@pytest.fixture
def log_stores(primary_client, shadow_client):
    """Create primary, shadow, and dual-write log stores."""
    from serving.storage.d1_log import D1LogStore

    primary_log = D1LogStore(primary_client)
    shadow_log = D1LogStore(shadow_client)
    dual_log = DualWriteLogStore(primary_log, shadow_log)
    return dual_log, primary_log, shadow_log


# ---------------------------------------------------------------------------
# Operational store: dual-write verification
# ---------------------------------------------------------------------------


class TestDualWriteUserSync:
    """Verify user writes land in both stores."""

    @pytest.mark.asyncio
    async def test_create_user_in_both(self, stores):
        """Create a user through dual-write; verify it exists in both backends."""
        dual, primary, shadow = stores

        await dual.create_user(
            user_id="u1",
            email="test@example.com",
            password_hash="hash123",
            user_name="Test User",
        )
        await _drain_shadow_tasks()

        p_user = await primary.get_user_by_id("u1")
        s_user = await shadow.get_user_by_id("u1")

        assert p_user is not None, "User missing from primary"
        assert s_user is not None, "User missing from shadow"
        assert p_user["email"] == "test@example.com"
        assert s_user["email"] == "test@example.com"
        assert p_user["user_name"] == "Test User"
        assert s_user["user_name"] == "Test User"

    @pytest.mark.asyncio
    async def test_update_user_fields_in_both(self, stores):
        """Update user fields; verify both stores reflect the change."""
        dual, primary, shadow = stores

        await dual.create_user(
            user_id="u2",
            email="u2@test.com",
            password_hash="hash",
        )
        await dual.update_user_fields("u2", user_name="Updated Name", role="admin")
        await _drain_shadow_tasks()

        p_user = await primary.get_user_by_id("u2")
        s_user = await shadow.get_user_by_id("u2")

        assert p_user["user_name"] == "Updated Name"
        assert s_user["user_name"] == "Updated Name"
        assert p_user["role"] == "admin"
        assert s_user["role"] == "admin"

    @pytest.mark.asyncio
    async def test_delete_user_in_both(self, stores):
        """Delete a user; verify both stores mark it as deleted."""
        dual, primary, shadow = stores

        await dual.create_user(
            user_id="u3",
            email="u3@test.com",
            password_hash="hash",
        )
        await dual.delete_user(
            "u3",
            admin_ip="127.0.0.1",
            admin_id="admin1",
            reason="test cleanup",
        )
        await _drain_shadow_tasks()

        p_user = await primary.get_user_by_id("u3")
        s_user = await shadow.get_user_by_id("u3")

        assert p_user["status"] == "deleted"
        assert s_user["status"] == "deleted"


class TestDualWriteKeySync:
    """Verify API key writes land in both stores."""

    @pytest.mark.asyncio
    async def test_create_key_in_both(self, stores):
        """Create a key through dual-write; verify in both backends."""
        dual, primary, shadow = stores

        await dual.create_user(
            user_id="ku1",
            email="ku1@test.com",
            password_hash="hash",
        )
        await dual.create_key(
            key_hash="keyhash1",
            key_prefix="sk-test",
            user_id="ku1",
            quota_daily_cost_usd=500.0,
            notes="test key",
        )
        await _drain_shadow_tasks()

        p_key = await primary.get_key_detail("ku1")
        s_key = await shadow.get_key_detail("ku1")

        assert p_key is not None, "Key missing from primary"
        assert s_key is not None, "Key missing from shadow"
        assert p_key["notes"] == "test key"
        assert s_key["notes"] == "test key"

    @pytest.mark.asyncio
    async def test_revoke_key_in_both(self, stores):
        """Revoke a key; verify both stores reflect revocation."""
        dual, primary, shadow = stores

        await dual.create_user(
            user_id="ku2",
            email="ku2@test.com",
            password_hash="hash",
        )
        await dual.create_key(
            key_hash="keyhash2",
            key_prefix="sk-rev",
            user_id="ku2",
        )
        await dual.revoke_key("ku2")
        await _drain_shadow_tasks()

        p_key = await primary.get_key_detail("ku2")
        s_key = await shadow.get_key_detail("ku2")

        assert p_key["status"] == "revoked"
        assert s_key["status"] == "revoked"


class TestDualWriteSessionSync:
    """Verify session writes land in both stores."""

    @pytest.mark.asyncio
    async def test_create_and_revoke_session_in_both(self, stores):
        """Create then revoke a session; verify both stores agree."""
        dual, primary, shadow = stores

        await dual.create_user(
            user_id="su1",
            email="su1@test.com",
            password_hash="hash",
        )

        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        await dual.create_session(
            session_id="sess1",
            user_id="su1",
            refresh_token_hash="rth1",
            jti="jti1",
            sid="sid1",
            expires_at=expires,
        )
        await _drain_shadow_tasks()

        p_sess = await primary.get_session_by_token_hash("rth1")
        s_sess = await shadow.get_session_by_token_hash("rth1")
        assert p_sess is not None, "Session missing from primary"
        assert s_sess is not None, "Session missing from shadow"

        await dual.revoke_session("sess1")
        await _drain_shadow_tasks()

        p_sess = await primary.get_session_by_token_hash("rth1")
        s_sess = await shadow.get_session_by_token_hash("rth1")
        assert p_sess["revoked"] == 1
        assert s_sess["revoked"] == 1


class TestDualWriteCostSync:
    """Verify cost counter writes land in both stores."""

    @pytest.mark.asyncio
    async def test_increment_cost_in_both(self, stores):
        """Increment user cost; verify both stores have the same total."""
        dual, primary, shadow = stores

        await dual.increment_user_cost("cu1", 1.50, day="2025-01-15")
        await dual.increment_user_cost("cu1", 2.25, day="2025-01-15")
        await _drain_shadow_tasks()

        p_cost = await primary.get_user_cost_period("cu1", "today")
        s_cost = await shadow.get_user_cost_period("cu1", "today")

        # Both should have the same value (exact match since same DB type)
        assert p_cost == s_cost


class TestDualWriteAuditSync:
    """Verify audit log writes land in both stores."""

    @pytest.mark.asyncio
    async def test_audit_log_in_both(self, stores):
        """Log an admin action; verify it appears in both stores."""
        dual, primary, shadow = stores

        await dual.log_admin_action(
            admin_ip="10.0.0.1",
            action="test_action",
            target_user_id="target1",
            details={"reason": "integration test"},
            success=True,
        )
        await _drain_shadow_tasks()

        p_count, p_rows = await primary.list_audit_log(action="test_action")
        s_count, s_rows = await shadow.list_audit_log(action="test_action")

        assert p_count >= 1
        assert s_count >= 1
        assert p_rows[0]["action"] == "test_action"
        assert s_rows[0]["action"] == "test_action"


class TestDualWriteReadIsolation:
    """Verify reads only come from primary, even when shadow has different data."""

    @pytest.mark.asyncio
    async def test_read_comes_from_primary_only(self, stores):
        """Write directly to shadow only; verify dual-write reads don't see it."""
        dual, _primary, shadow = stores

        # Write directly to shadow (bypassing dual-write)
        await shadow.create_user(
            user_id="shadow_only",
            email="shadow@test.com",
            password_hash="hash",
        )

        # Dual-write read should NOT find it (reads go to primary only)
        result = await dual.get_user_by_id("shadow_only")
        assert result is None, "Dual-write read leaked to shadow store"

        # Shadow should have it directly
        s_user = await shadow.get_user_by_id("shadow_only")
        assert s_user is not None


class TestDualWriteShadowFailure:
    """Verify primary succeeds even when shadow is broken."""

    @pytest.mark.asyncio
    async def test_primary_succeeds_when_shadow_db_broken(self, primary_client):
        """Break the shadow DB; verify primary writes still succeed."""
        from serving.storage.d1_operational import D1OperationalStore

        # Primary is healthy
        primary_store = D1OperationalStore(primary_client)

        # Shadow has a broken/closed connection
        broken_client = _SqliteD1Client()
        _load_schema(broken_client)
        broken_store = D1OperationalStore(broken_client)
        broken_client._conn.close()  # break it

        dual = DualWriteOperationalStore(primary_store, broken_store)

        # Should succeed (shadow failure swallowed)
        await dual.create_user(
            user_id="resilient",
            email="r@test.com",
            password_hash="hash",
        )
        await _drain_shadow_tasks()

        p_user = await primary_store.get_user_by_id("resilient")
        assert p_user is not None
        assert p_user["email"] == "r@test.com"
        assert not dual.shadow_healthy


# ---------------------------------------------------------------------------
# Log store: dual-write verification
# ---------------------------------------------------------------------------


class TestDualWriteLogSync:
    """Verify log_request writes land in both stores."""

    @pytest.mark.asyncio
    async def test_log_request_in_both(self, log_stores):
        """Write a log entry; verify both stores have it."""
        dual, primary, shadow = log_stores

        await dual.initialize()

        await dual.log_request(
            request_id="req1",
            model_id="gpt-4",
            provider="openai",
            prompt="hello",
            response=None,
            usage={"prompt_tokens": 5, "completion_tokens": 10},
            latency_ms=150,
            status_code=200,
            metadata={"user_id": "log_u1"},
        )
        await _drain_shadow_tasks()

        # Flush both stores' buffers
        if hasattr(primary, "flush"):
            await primary.flush()
        if hasattr(shadow, "flush"):
            await shadow.flush()

        # Verify via direct SQL on both clients
        p_result = await primary._d1.query("SELECT * FROM api_logs WHERE request_id = ?", ["req1"])
        s_result = await shadow._d1.query("SELECT * FROM api_logs WHERE request_id = ?", ["req1"])

        assert len(p_result.rows) == 1, "Log missing from primary"
        assert len(s_result.rows) == 1, "Log missing from shadow"
        assert p_result.rows[0]["model_id"] == "gpt-4"
        assert s_result.rows[0]["model_id"] == "gpt-4"
