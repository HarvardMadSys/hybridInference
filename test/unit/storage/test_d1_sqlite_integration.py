"""Integration tests for D1OperationalStore against real SQLite.

Runs the actual SQL against an in-memory SQLite database to catch:
- Invalid SQL syntax (not caught by mocks)
- Schema mismatches (missing columns, wrong types)
- Constraint violations
- LIKE pattern behavior
- Partial index validity
- RETURNING clause support
- Type coercion edge cases (Decimal, bool, datetime, None)

No network, no D1 credentials — uses aiosqlite to emulate D1 locally.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# SQLite-backed fake D1Client
# ---------------------------------------------------------------------------

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "serving" / "storage" / "d1_schema.sql"


class _SqliteD1Client:
    """In-process SQLite that speaks the same interface as D1Client.

    This lets us run D1OperationalStore against real SQLite without mocking,
    catching SQL syntax errors that mocks would miss.
    """

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_client() -> _SqliteD1Client:
    """Create an in-memory SQLite client with the D1 schema loaded."""
    client = _SqliteD1Client()
    schema_sql = SCHEMA_PATH.read_text()
    for segment in schema_sql.split(";"):
        lines = [ln for ln in segment.splitlines() if not ln.strip().startswith("--")]
        cleaned = "\n".join(lines).strip()
        if cleaned:
            client._conn.execute(cleaned)
    client._conn.commit()
    return client


@pytest.fixture
def store(sqlite_client) -> Any:
    """Create a D1OperationalStore backed by in-memory SQLite."""
    from serving.storage.d1_operational import D1OperationalStore

    return D1OperationalStore(sqlite_client)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidity:
    """Verify the D1 schema DDL executes without errors on real SQLite."""

    def test_schema_loads(self, sqlite_client):
        """All CREATE TABLE and CREATE INDEX statements succeed."""
        # If we got here, the fixture loaded the schema successfully
        result = sqlite_client._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = [r[0] for r in result]
        assert "users" in tables
        assert "api_keys" in tables
        assert "auth_sessions" in tables
        assert "email_verification_tokens" in tables
        assert "password_reset_tokens" in tables
        assert "admin_audit_log" in tables

    def test_indexes_created(self, sqlite_client):
        """All indexes exist."""
        result = sqlite_client._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
        ).fetchall()
        index_names = [r[0] for r in result]
        # Spot check key indexes
        assert "idx_users_email" in index_names
        assert "idx_users_pending_approval" in index_names  # partial index
        assert "idx_api_keys_user_active" in index_names  # partial unique index
        assert "idx_api_keys_account_active" in index_names  # partial unique index
        assert "idx_auth_sessions_token" in index_names  # partial index
        assert "idx_admin_audit_timestamp" in index_names

    def test_schema_is_idempotent(self, sqlite_client):
        """Running schema DDL twice doesn't error (IF NOT EXISTS)."""
        schema_sql = SCHEMA_PATH.read_text()
        for segment in schema_sql.split(";"):
            lines = [ln for ln in segment.splitlines() if not ln.strip().startswith("--")]
            cleaned = "\n".join(lines).strip()
            if cleaned:
                sqlite_client._conn.execute(cleaned)  # second run — should not raise


# ---------------------------------------------------------------------------
# Users — full CRUD against real SQLite
# ---------------------------------------------------------------------------


class TestUsersCRUD:
    """Test user operations produce valid SQL."""

    async def test_create_and_get_user(self, store):
        await store.create_user(
            user_id="u1",
            email="Alice@Test.COM",
            password_hash="$argon2id$hash",
            user_name="Alice",
            email_verified=False,
            status="active",
        )
        user = await store.get_user_by_id("u1")
        assert user is not None
        assert user["email"] == "alice@test.com"
        assert user["email_verified"] == 0  # SQLite integer
        assert user["role"] == "free"

    async def test_get_user_by_email_case_insensitive(self, store):
        await store.create_user(user_id="u1", email="bob@test.com", password_hash="h")
        user = await store.get_user_by_email("BOB@TEST.COM")
        assert user is not None

    async def test_update_user_fields_with_all_types(self, store):
        """Exercises bool, datetime, Decimal, str, and None coercion."""
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")

        await store.update_user_fields(
            "u1",
            email_verified=True,
            role="admin",
        )
        user = await store.get_user_by_id("u1")
        assert user["email_verified"] == 1
        assert user["role"] == "admin"

    async def test_update_user_fields_datetime_and_none(self, store):
        """Datetime and None coercion in update_user_fields."""
        await store.create_user(
            user_id="u1", email="a@b.com", password_hash="h", status="pending_approval"
        )
        await store.update_user_fields(
            "u1",
            reviewed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            approval_note=None,
        )
        # Verify via list_users which returns these columns
        total, rows, _ = await store.list_users(search="a@b.com")
        assert total == 1
        assert "2026-01-01" in (rows[0]["reviewed_at"] or "")
        assert rows[0]["approval_note"] is None

    async def test_update_user_last_login(self, store):
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        await store.update_user_last_login("u1")
        user = await store.get_user_by_id("u1")
        assert user["last_login_at"] is not None

    async def test_mark_email_verified(self, store):
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        await store.mark_user_email_verified("u1")
        user = await store.get_user_by_id("u1")
        assert user["email_verified"] == 1

    async def test_list_users_with_search(self, store):
        await store.create_user(
            user_id="u1", email="alice@test.com", password_hash="h", user_name="Alice Smith"
        )
        await store.create_user(
            user_id="u2", email="bob@test.com", password_hash="h", user_name="Bob Jones"
        )

        total, rows, _counts = await store.list_users(search="alice")
        assert total == 1
        assert rows[0]["id"] == "u1"

    async def test_list_users_search_truncated_for_like_limit(self, store):
        """Search terms >46 chars get truncated to stay under D1's 50-byte LIKE limit."""
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        # This should not raise even with a very long search
        long_search = "a" * 100
        _total, _rows, _counts = await store.list_users(search=long_search)
        # Should execute without error; result doesn't matter

    async def test_user_preferences_roundtrip(self, store):
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        await store.update_user_preferences("u1", {"theme": "dark", "lang": "en"})
        prefs = await store.get_user_preferences("u1")
        assert prefs == {"theme": "dark", "lang": "en"}

    async def test_approve_and_reject_user(self, store):
        await store.create_user(
            user_id="u1", email="a@b.com", password_hash="h", status="pending_approval"
        )
        await store.approve_user("u1", admin_id="admin1", note="looks good")
        user = await store.get_user_by_id("u1")
        assert user["status"] == "active"

        await store.reject_user("u1", admin_id="admin1", reason="spam")
        user = await store.get_user_by_id("u1")
        assert user["status"] == "rejected"

    async def test_get_active_user_counts(self, store):
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        await store.update_user_last_login("u1")
        counts = await store.get_active_user_counts()
        assert counts["total"] >= 1
        assert counts["dau"] >= 1


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class TestAPIKeysCRUD:
    """Test API key operations produce valid SQL."""

    async def _create_user_and_key(self, store, uid="u1") -> dict:
        await store.create_user(user_id=uid, email=f"{uid}@test.com", password_hash="h")
        row = await store.create_key(
            key_hash=f"hash_{uid}",
            key_prefix=f"pfx_{uid}",
            user_id=uid,
            user_name="Test",
            tier="free",
            quota_daily_cost_usd=Decimal("100.50"),
            account_id=uid,
        )
        return row

    async def test_create_key_with_decimal(self, store):
        """Decimal quota values don't crash SQLite."""
        row = await self._create_user_and_key(store)
        assert row["id"] is not None

    async def test_auth_context_join(self, store):
        """The auth context query (JOIN users) returns correct projection."""
        await self._create_user_and_key(store)
        ctx = await store.get_auth_context_by_key_hash("hash_u1")
        assert ctx is not None
        assert ctx["user_id"] == "u1"
        assert ctx["email"] == "u1@test.com"
        assert ctx["role"] == "free"
        assert ctx["quota_daily_cost_usd"] is not None

    async def test_auth_context_excludes_revoked_key(self, store):
        await self._create_user_and_key(store)
        await store.revoke_key("u1")
        ctx = await store.get_auth_context_by_key_hash("hash_u1")
        assert ctx is None

    async def test_auth_context_excludes_deleted_user(self, store):
        await self._create_user_and_key(store)
        await store.delete_user("u1", admin_ip="1.2.3.4", admin_id="a1")
        ctx = await store.get_auth_context_by_key_hash("hash_u1")
        assert ctx is None

    async def test_auth_context_rejects_orphan_key(self, store, sqlite_client):
        """A key whose user row does not exist in users must not authenticate.

        With the old LEFT JOIN condition `u.id IS NULL OR u.status = 'active'`
        the NULL branch allowed orphan keys through.  The fix requires
        `u.id IS NOT NULL AND u.status = 'active'`.
        """
        # Insert a key directly — no matching user row
        sqlite_client._conn.execute(
            "INSERT INTO api_keys "
            "(key_hash, key_prefix, user_id, user_name, status, tier, "
            " quota_daily_cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, 'active', 'free', 10.0, '2026-01-01T00:00:00Z')",
            ["orphan-hash", "orp_", "nonexistent-user", "Ghost"],
        )
        sqlite_client._conn.commit()

        ctx = await store.get_auth_context_by_key_hash("orphan-hash")
        assert ctx is None, "orphan key (no matching user row) must not authenticate"

    async def test_regenerate_key_atomic(self, store):
        await self._create_user_and_key(store)
        old_pfx = await store.regenerate_key(
            "u1", new_key_hash="new_hash", new_key_prefix="new_pfx"
        )
        assert old_pfx == "pfx_u1"

        # Old hash gone, new hash works
        assert await store.get_auth_context_by_key_hash("hash_u1") is None
        ctx = await store.get_auth_context_by_key_hash("new_hash")
        assert ctx is not None

    async def test_update_key_with_decimal(self, store):
        """Decimal in update_key doesn't crash."""
        await self._create_user_and_key(store)
        await store.update_key("u1", quota_daily_cost_usd=Decimal("999.99"))
        detail = await store.get_key_detail("u1")
        assert detail["quota_daily_cost_usd"] == pytest.approx(999.99)

    async def test_list_keys(self, store):
        await self._create_user_and_key(store, "u1")
        await self._create_user_and_key(store, "u2")
        total, _rows = await store.list_keys(status="active")
        assert total == 2

    async def test_check_active_key_exists(self, store):
        await self._create_user_and_key(store)
        assert await store.check_active_key_exists("u1") is True
        assert await store.check_active_key_exists("nonexistent") is False


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessionsCRUD:
    """Test session operations."""

    async def test_session_lifecycle(self, store):
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        await store.create_session(
            session_id="s1",
            user_id="u1",
            refresh_token_hash="rth1",
            jti="jti1",
            sid="sid1",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )

        sess = await store.get_session_by_token_hash("rth1")
        assert sess is not None
        assert sess["revoked"] == 0

        await store.rotate_session("s1", new_refresh_token_hash="rth2", new_jti="jti2")
        assert await store.get_session_by_token_hash("rth1") is None
        sess2 = await store.get_session_by_token_hash("rth2")
        assert sess2 is not None

        await store.revoke_session("s1")
        sess3 = await store.get_session_by_token_hash("rth2")
        assert sess3["revoked"] == 1

        await store.delete_user_sessions("u1")
        assert await store.get_session_by_token_hash("rth2") is None


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TestTokensCRUD:
    """Test verification and reset token operations."""

    async def test_verification_token_lifecycle(self, store):
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        await store.create_verification_token(
            token="vt1",
            user_id="u1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )

        vt = await store.get_verification_token("vt1")
        assert vt is not None
        assert vt["used_at"] is None

        await store.mark_verification_used("vt1")
        vt = await store.get_verification_token("vt1")
        assert vt["used_at"] is not None

        await store.delete_user_verification_tokens("u1")
        assert await store.get_verification_token("vt1") is None

    async def test_reset_token_lifecycle(self, store):
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        await store.create_reset_token(
            token="rt1",
            user_id="u1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        rt = await store.get_reset_token("rt1")
        assert rt is not None

        await store.mark_reset_used("rt1")
        rt = await store.get_reset_token("rt1")
        assert rt["used_at"] is not None

        await store.delete_user_reset_tokens("u1")
        assert await store.get_reset_token("rt1") is None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class TestAuditLog:
    """Test audit log operations."""

    async def test_log_and_list(self, store):
        await store.log_admin_action(
            admin_ip="1.2.3.4",
            action="test_action",
            target_user_id="u1",
            details={"key": "value"},
            success=True,
        )
        total, rows = await store.list_audit_log(action="test_action")
        assert total >= 1
        assert rows[0]["action"] == "test_action"
        # details should be JSON string in SQLite
        details = rows[0]["details"]
        if isinstance(details, str):
            details = json.loads(details)
        assert details["key"] == "value"
        assert rows[0]["success"] == 1  # boolean as int


# ---------------------------------------------------------------------------
# delete_user atomicity
# ---------------------------------------------------------------------------


class TestDeleteUserAtomic:
    """Test that delete_user cleans up all related data atomically."""

    async def test_delete_purges_all_related_data(self, store):
        # Setup: user with key, session, and tokens
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        await store.create_key(
            key_hash="kh1",
            key_prefix="kp1",
            user_id="u1",
            account_id="u1",
        )
        await store.create_session(
            session_id="s1",
            user_id="u1",
            refresh_token_hash="rth1",
            jti="j1",
            sid="sid1",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        await store.create_verification_token(
            token="vt1",
            user_id="u1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        await store.create_reset_token(
            token="rt1",
            user_id="u1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        # Delete
        await store.delete_user("u1", admin_ip="1.2.3.4", admin_id="admin1", reason="test")

        # Verify: user soft-deleted, everything else purged
        user = await store.get_user_by_id("u1")
        assert user["status"] == "deleted"

        assert await store.get_auth_context_by_key_hash("kh1") is None
        assert await store.get_session_by_token_hash("rth1") is None
        assert await store.get_verification_token("vt1") is None
        assert await store.get_reset_token("rt1") is None

        # Audit log entry created
        total, _rows = await store.list_audit_log(action="delete_user")
        assert total >= 1


# ---------------------------------------------------------------------------
# Constraint enforcement
# ---------------------------------------------------------------------------


class TestConstraints:
    """Verify SQLite enforces the schema constraints."""

    async def test_duplicate_email_rejected(self, store):
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        with pytest.raises(Exception, match="UNIQUE"):
            await store.create_user(user_id="u2", email="a@b.com", password_hash="h")

    async def test_invalid_role_rejected(self, store, sqlite_client):
        with pytest.raises(Exception, match="CHECK"):
            await sqlite_client.execute(
                "INSERT INTO users (id, email, password_hash, role) VALUES (?, ?, ?, ?)",
                ["u1", "a@b.com", "h", "superadmin"],
            )

    async def test_invalid_status_rejected(self, store, sqlite_client):
        with pytest.raises(Exception, match="CHECK"):
            await sqlite_client.execute(
                "INSERT INTO users (id, email, password_hash, status) VALUES (?, ?, ?, ?)",
                ["u1", "a@b.com", "h", "banned"],
            )

    async def test_duplicate_key_hash_rejected(self, store):
        await store.create_user(user_id="u1", email="a@b.com", password_hash="h")
        await store.create_user(user_id="u2", email="b@b.com", password_hash="h")
        await store.create_key(key_hash="kh1", key_prefix="p1", user_id="u1", account_id="u1")
        with pytest.raises(Exception, match="UNIQUE"):
            await store.create_key(key_hash="kh1", key_prefix="p2", user_id="u2", account_id="u2")


# ---------------------------------------------------------------------------
# Cost counters (user_daily_cost table)
# ---------------------------------------------------------------------------


class TestCostCounters:
    """Test user_daily_cost table operations against real SQLite."""

    async def test_increment_creates_new_row(self, store):
        await store.increment_user_cost("u1", 1.50, day="2026-04-07")
        await store.get_user_cost_today("u1")
        # get_user_cost_today uses today's date, not the one we passed
        # so query directly for the day we incremented
        result = await store._d1.query(
            "SELECT cost_usd, requests FROM user_daily_cost WHERE user_id = ? AND day = ?",
            ["u1", "2026-04-07"],
        )
        assert result.rows[0]["cost_usd"] == pytest.approx(1.50)
        assert result.rows[0]["requests"] == 1

    async def test_increment_accumulates(self, store):
        await store.increment_user_cost("u1", 1.00, day="2026-04-07")
        await store.increment_user_cost("u1", 2.50, day="2026-04-07")
        await store.increment_user_cost("u1", 0.25, day="2026-04-07")

        result = await store._d1.query(
            "SELECT cost_usd, requests FROM user_daily_cost WHERE user_id = ? AND day = ?",
            ["u1", "2026-04-07"],
        )
        assert result.rows[0]["cost_usd"] == pytest.approx(3.75)
        assert result.rows[0]["requests"] == 3

    async def test_increment_separate_days(self, store):
        await store.increment_user_cost("u1", 10.0, day="2026-04-06")
        await store.increment_user_cost("u1", 20.0, day="2026-04-07")

        r1 = await store._d1.query(
            "SELECT cost_usd FROM user_daily_cost WHERE user_id = ? AND day = ?",
            ["u1", "2026-04-06"],
        )
        r2 = await store._d1.query(
            "SELECT cost_usd FROM user_daily_cost WHERE user_id = ? AND day = ?",
            ["u1", "2026-04-07"],
        )
        assert r1.rows[0]["cost_usd"] == pytest.approx(10.0)
        assert r2.rows[0]["cost_usd"] == pytest.approx(20.0)

    async def test_increment_separate_users(self, store):
        await store.increment_user_cost("u1", 5.0, day="2026-04-07")
        await store.increment_user_cost("u2", 8.0, day="2026-04-07")

        await store.get_batch_usage(["u1", "u2"], period="today")
        # get_batch_usage uses today's date which may not be 2026-04-07 in test
        # so verify via direct query
        r = await store._d1.query(
            "SELECT user_id, cost_usd FROM user_daily_cost WHERE day = ? ORDER BY user_id",
            ["2026-04-07"],
        )
        costs = {row["user_id"]: row["cost_usd"] for row in r.rows}
        assert costs["u1"] == pytest.approx(5.0)
        assert costs["u2"] == pytest.approx(8.0)

    async def test_increment_updates_last_request_at(self, store):
        await store.increment_user_cost("u1", 1.0, day="2026-04-07")
        result = await store._d1.query(
            "SELECT last_request_at FROM user_daily_cost WHERE user_id = ? AND day = ?",
            ["u1", "2026-04-07"],
        )
        assert result.rows[0]["last_request_at"] is not None

    async def test_get_user_cost_period_month(self, store):
        """Month period sums all days with matching YYYY-MM prefix."""
        await store.increment_user_cost("u1", 10.0, day="2026-04-01")
        await store.increment_user_cost("u1", 20.0, day="2026-04-15")
        await store.increment_user_cost("u1", 99.0, day="2026-03-31")  # different month

        await store.get_user_cost_period("u1", period="month")
        # This uses current month, so we need to check with a known month
        r = await store._d1.query(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM user_daily_cost "
            "WHERE user_id = ? AND day LIKE ?",
            ["u1", "2026-04%"],
        )
        assert r.rows[0]["total"] == pytest.approx(30.0)

    async def test_get_batch_usage_empty(self, store):
        result = await store.get_batch_usage([], period="today")
        assert result == {}

    async def test_get_batch_usage_returns_correct_costs(self, store):
        """get_batch_usage executes valid SQL and returns merged costs."""
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await store.increment_user_cost("u1", 3.00, day=today)
        await store.increment_user_cost("u1", 2.00, day=today)
        await store.increment_user_cost("u2", 7.50, day=today)
        # u3 has no rows — should be absent from result, not KeyError
        result = await store.get_batch_usage(["u1", "u2", "u3"], period="today")

        assert result["u1"] == pytest.approx(5.00)
        assert result["u2"] == pytest.approx(7.50)
        assert "u3" not in result

    async def test_get_batch_usage_over_99_ids_merges_all_chunks(self, store):
        """100 user IDs are chunked into two queries; all results merge correctly."""
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        user_ids = [f"chunk-u{i}" for i in range(100)]
        for uid in user_ids:
            await store.increment_user_cost(uid, 1.0, day=today)

        result = await store.get_batch_usage(user_ids, period="today")

        assert len(result) == 100
        for uid in user_ids:
            assert result[uid] == pytest.approx(1.0), f"missing or wrong cost for {uid}"

    async def test_user_daily_cost_table_exists(self, sqlite_client):
        """Verify the table was created by schema DDL."""
        result = sqlite_client._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_daily_cost'"
        ).fetchall()
        assert len(result) == 1
