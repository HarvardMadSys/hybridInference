"""Unit tests for D1OperationalStore.

All tests mock the D1Client — no real D1 calls are made.
Verifies correct SQL dialect translations and parameter mapping.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.d1_client import D1Result
from serving.storage.d1_operational import D1OperationalStore


@pytest.fixture
def d1_client() -> MagicMock:
    """Create a mock D1Client."""
    client = MagicMock()
    client.query = AsyncMock(return_value=D1Result())
    client.execute = AsyncMock(return_value=D1Result())
    client.batch = AsyncMock(return_value=[])
    client.health_check = AsyncMock(return_value=True)
    client.close = AsyncMock()
    return client


@pytest.fixture
def store(d1_client: MagicMock) -> D1OperationalStore:
    """Create a D1OperationalStore with mock client."""
    return D1OperationalStore(d1_client)


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


class TestLifecycle:
    """Tests for initialize, cleanup, health_check."""

    async def test_health_check_delegates(self, store, d1_client):
        assert await store.health_check() is True
        d1_client.health_check.assert_awaited_once()

    async def test_cleanup_closes_client(self, store, d1_client):
        await store.cleanup()
        d1_client.close.assert_awaited_once()


# ------------------------------------------------------------------
# Users — reads
# ------------------------------------------------------------------


class TestUserReads:
    """Tests for user read methods."""

    async def test_get_user_by_id(self, store, d1_client):
        d1_client.query.return_value = D1Result(
            rows=[{"id": "u1", "email": "a@b.com", "role": "admin"}]
        )
        result = await store.get_user_by_id("u1")
        assert result["id"] == "u1"

        sql = d1_client.query.call_args[0][0]
        assert "WHERE id = ?" in sql
        assert d1_client.query.call_args[0][1] == ["u1"]

    async def test_get_user_by_id_not_found(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[])
        result = await store.get_user_by_id("nonexistent")
        assert result is None

    async def test_get_user_by_email_lowercases(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[])
        await store.get_user_by_email("Alice@Example.COM")
        params = d1_client.query.call_args[0][1]
        assert params == ["alice@example.com"]

    async def test_get_user_counts_by_status(self, store, d1_client):
        d1_client.query.return_value = D1Result(
            rows=[{"status": "active", "cnt": 10}, {"status": "suspended", "cnt": 2}]
        )
        result = await store.get_user_counts_by_status()
        assert result == {"active": 10, "suspended": 2}

    async def test_get_active_user_counts(self, store, d1_client):
        d1_client.query.side_effect = [
            D1Result(rows=[{"count": 100}]),
            D1Result(rows=[{"count": 20}]),
            D1Result(rows=[{"count": 50}]),
        ]
        result = await store.get_active_user_counts()
        assert result == {"total": 100, "dau": 20, "mau": 50}

        # Verify ISO-format strftime used so comparisons work with stored timestamps
        dau_sql = d1_client.query.call_args_list[1][0][0]
        assert "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-24 hours')" in dau_sql


# ------------------------------------------------------------------
# Users — writes
# ------------------------------------------------------------------


class TestUserWrites:
    """Tests for user write methods."""

    async def test_create_user(self, store, d1_client):
        await store.create_user(
            user_id="u1",
            email="Alice@Test.COM",
            password_hash="hash",
            user_name="Alice",
        )
        d1_client.execute.assert_awaited_once()
        sql, params = d1_client.execute.call_args[0]
        assert "INSERT INTO users" in sql
        assert params[0] == "u1"
        assert params[1] == "alice@test.com"  # lowercased
        assert params[4] == 0  # email_verified as int

    async def test_update_user_fields_bool_conversion(self, store, d1_client):
        await store.update_user_fields("u1", email_verified=True, status="active")
        sql, params = d1_client.execute.call_args[0]
        assert "email_verified = ?" in sql
        assert "status = ?" in sql
        # Bool should be converted to int
        assert 1 in params
        assert "active" in params

    async def test_update_user_fields_empty_noop(self, store, d1_client):
        await store.update_user_fields("u1")
        d1_client.execute.assert_not_awaited()

    async def test_update_user_last_login(self, store, d1_client):
        await store.update_user_last_login("u1")
        sql = d1_client.execute.call_args[0][0]
        assert "last_login_at = ?" in sql


# ------------------------------------------------------------------
# Users — delete (atomic batch)
# ------------------------------------------------------------------


class TestDeleteUser:
    """Tests for the atomic delete_user batch."""

    async def test_delete_user_uses_batch(self, store, d1_client):
        await store.delete_user("u1", admin_ip="1.2.3.4", admin_id="admin1", reason="spam")
        d1_client.batch.assert_awaited_once()

        statements = d1_client.batch.call_args[0][0]
        # Should have 6 statements: update user, revoke keys, delete sessions,
        # delete email tokens, delete reset tokens, insert audit log
        assert len(statements) == 6
        assert "UPDATE users SET status = 'deleted'" in statements[0][0]
        assert "UPDATE api_keys SET status = 'revoked'" in statements[1][0]
        assert "DELETE FROM auth_sessions" in statements[2][0]
        assert "INSERT INTO admin_audit_log" in statements[5][0]


# ------------------------------------------------------------------
# Users — list
# ------------------------------------------------------------------


class TestListUsers:
    """Tests for list_users."""

    async def test_list_users_basic(self, store, d1_client):
        d1_client.query.side_effect = [
            D1Result(rows=[{"total": 5}]),  # count
            D1Result(rows=[{"status": "active", "cnt": 5}]),  # status counts
            D1Result(rows=[{"id": "u1"}]),  # main query
        ]
        total, rows, status_counts = await store.list_users()
        assert total == 5
        assert len(rows) == 1
        assert status_counts["active"] == 5

    async def test_list_users_with_search_uses_like(self, store, d1_client):
        d1_client.query.side_effect = [
            D1Result(rows=[{"total": 0}]),
            D1Result(rows=[]),
            D1Result(rows=[]),
        ]
        await store.list_users(search="alice")

        # First query (count) should have LIKE params
        count_params = d1_client.query.call_args_list[0][0][1]
        assert "%alice%" in count_params

    async def test_cost_sort_falls_back(self, store, d1_client):
        """Cost-based sort falls back to created_at (D1 has no api_logs)."""
        d1_client.query.side_effect = [
            D1Result(rows=[{"total": 0}]),
            D1Result(rows=[]),
            D1Result(rows=[]),
        ]
        await store.list_users(sort_by="cost_today")

        # Main query should use created_at ordering
        main_sql = d1_client.query.call_args_list[2][0][0]
        assert "created_at DESC" in main_sql


# ------------------------------------------------------------------
# API keys
# ------------------------------------------------------------------


class TestAPIKeys:
    """Tests for API key methods."""

    async def test_get_auth_context_by_key_hash(self, store, d1_client):
        d1_client.query.return_value = D1Result(
            rows=[{"id": 1, "user_id": "u1", "email": "a@b.com", "role": "admin"}]
        )
        result = await store.get_auth_context_by_key_hash("hash123")
        assert result["user_id"] == "u1"

        sql = d1_client.query.call_args[0][0]
        # Should use ISO-format strftime and require a joined user row
        assert "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')" in sql
        assert "JOIN users" in sql
        assert "u.id IS NOT NULL" in sql

    async def test_get_auth_context_by_key_hash_selects_email_verified(self, store, d1_client):
        """Projection must include u.email_verified for SIGNUP_REQUIRE_EMAIL_VERIFICATION."""
        d1_client.query.return_value = D1Result(
            rows=[
                {
                    "id": 1,
                    "user_id": "u1",
                    "email": "a@b.com",
                    "role": "free",
                    "email_verified": 1,
                }
            ]
        )
        result = await store.get_auth_context_by_key_hash("hash123")
        sql = d1_client.query.call_args[0][0]
        assert "u.email_verified" in sql
        # _parse_row_timestamps coerces SQLite int (0/1) to bool
        assert result["email_verified"] is True
        assert isinstance(result["email_verified"], bool)

    async def test_get_auth_context_lightweight_selects_email_verified(self, store, d1_client):
        """Lightweight projection must also include u.email_verified."""
        d1_client.query.return_value = D1Result(
            rows=[{"user_id": "u1", "email": "a@b.com", "role": "free", "email_verified": 0}]
        )
        result = await store.get_auth_context_lightweight("hash123")
        sql = d1_client.query.call_args[0][0]
        assert "u.email_verified" in sql
        assert result["email_verified"] is False
        assert isinstance(result["email_verified"], bool)

    async def test_check_active_key_exists_true(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[{"id": 1}])
        assert await store.check_active_key_exists("u1") is True

    async def test_check_active_key_exists_false(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[])
        assert await store.check_active_key_exists("u1") is False

    async def test_create_key_returns_row(self, store, d1_client):
        d1_client.query.return_value = D1Result(
            rows=[{"id": 42, "created_at": "2026-01-01T00:00:00Z"}]
        )
        result = await store.create_key(key_hash="h", key_prefix="pfx", user_id="u1")
        assert result["id"] == 42

    async def test_regenerate_key_returns_old_prefix(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[{"key_prefix": "old-pfx"}])
        old = await store.regenerate_key("u1", new_key_hash="new-h", new_key_prefix="new-pfx")
        assert old == "old-pfx"

    async def test_regenerate_key_no_key_raises(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[])
        with pytest.raises(ValueError, match="No key found"):
            await store.regenerate_key("u1", new_key_hash="h", new_key_prefix="p")

    async def test_revoke_key_soft(self, store, d1_client):
        await store.revoke_key("u1")
        sql = d1_client.execute.call_args[0][0]
        assert "status = 'revoked'" in sql

    async def test_revoke_key_hard(self, store, d1_client):
        await store.revoke_key("u1", hard_delete=True)
        sql = d1_client.execute.call_args[0][0]
        assert "DELETE FROM api_keys" in sql


# ------------------------------------------------------------------
# Sessions
# ------------------------------------------------------------------


class TestSessions:
    """Tests for auth session methods."""

    async def test_create_session(self, store, d1_client):
        await store.create_session(
            session_id="s1",
            user_id="u1",
            refresh_token_hash="rth",
            jti="j1",
            sid="sid1",
            expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        _, params = d1_client.execute.call_args[0]
        # revoked should be 0 (int, not False)
        assert params[-1] == 0

    async def test_get_session_by_token_hash(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[{"id": "s1", "user_id": "u1", "revoked": 0}])
        result = await store.get_session_by_token_hash("rth")
        assert result["id"] == "s1"

    async def test_revoke_session_uses_int(self, store, d1_client):
        await store.revoke_session("s1")
        sql = d1_client.execute.call_args[0][0]
        assert "revoked = 1" in sql


# ------------------------------------------------------------------
# Tokens
# ------------------------------------------------------------------


class TestTokens:
    """Tests for verification and reset token methods."""

    async def test_create_verification_token(self, store, d1_client):
        await store.create_verification_token(
            token="t1",
            user_id="u1",
            expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        d1_client.execute.assert_awaited_once()

    async def test_get_verification_token(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[{"token": "t1", "user_id": "u1"}])
        result = await store.get_verification_token("t1")
        assert result["token"] == "t1"

    async def test_mark_user_email_verified_uses_int(self, store, d1_client):
        await store.mark_user_email_verified("u1")
        sql = d1_client.execute.call_args[0][0]
        assert "email_verified = 1" in sql

    async def test_get_reset_token(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[{"token": "t1"}])
        result = await store.get_reset_token("t1")
        assert result["token"] == "t1"


# ------------------------------------------------------------------
# Audit log
# ------------------------------------------------------------------


class TestAuditLog:
    """Tests for admin audit log methods."""

    async def test_log_admin_action(self, store, d1_client):
        await store.log_admin_action(
            admin_ip="1.2.3.4",
            action="delete_user",
            target_user_id="u1",
            details={"reason": "spam"},
            success=True,
        )
        _, params = d1_client.execute.call_args[0]
        # success should be int(True) = 1
        assert params[-1] == 1
        # details should be JSON string
        assert '"reason": "spam"' in params[4]

    async def test_list_audit_log_with_filters(self, store, d1_client):
        d1_client.query.side_effect = [
            D1Result(rows=[{"total": 1}]),
            D1Result(rows=[{"id": 1, "action": "delete_user"}]),
        ]
        total, _rows = await store.list_audit_log(action="delete_user", limit=10)
        assert total == 1

        # Verify WHERE clause uses ?
        count_sql = d1_client.query.call_args_list[0][0][0]
        assert "action = ?" in count_sql


# ------------------------------------------------------------------
# Preferences
# ------------------------------------------------------------------


class TestPreferences:
    """Tests for user preferences methods."""

    async def test_get_preferences_parses_json_string(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[{"preferences": '{"theme": "dark"}'}])
        result = await store.get_user_preferences("u1")
        assert result == {"theme": "dark"}

    async def test_get_preferences_empty_on_missing_user(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[])
        result = await store.get_user_preferences("u1")
        assert result == {}

    async def test_update_preferences_serializes_json(self, store, d1_client):
        await store.update_user_preferences("u1", {"theme": "dark"})
        _, params = d1_client.execute.call_args[0]
        assert params[0] == '{"theme": "dark"}'


# ------------------------------------------------------------------
# Protocol compliance
# ------------------------------------------------------------------


class TestColumnAllowlist:
    """Verify that update methods reject invalid column names."""

    async def test_update_user_fields_rejects_invalid_column(self, store, d1_client):
        with pytest.raises(ValueError, match="Invalid column"):
            await store.update_user_fields("user-1", foo="bar")
        d1_client.execute.assert_not_called()

    async def test_update_key_rejects_invalid_column(self, store, d1_client):
        with pytest.raises(ValueError, match="Invalid column"):
            await store.update_key("user-1", foo="bar")
        d1_client.execute.assert_not_called()

    async def test_update_user_fields_accepts_valid_column(self, store, d1_client):
        await store.update_user_fields("user-1", status="active")
        d1_client.execute.assert_called_once()

    async def test_update_key_accepts_valid_column(self, store, d1_client):
        await store.update_key("user-1", status="active")
        d1_client.execute.assert_called_once()


class TestBatchUsage:
    """Tests for get_batch_usage, including D1 100-param chunking."""

    async def test_get_batch_usage_single_chunk(self, store, d1_client):
        """Fewer than 100 user IDs fit in one query."""
        d1_client.query.return_value = D1Result(
            rows=[{"user_id": "u1", "cost": 1.5}, {"user_id": "u2", "cost": 0.5}]
        )
        result = await store.get_batch_usage(["u1", "u2"], "today")
        assert result == {"u1": 1.5, "u2": 0.5}
        d1_client.query.assert_awaited_once()

    async def test_get_batch_usage_empty_returns_empty(self, store, d1_client):
        result = await store.get_batch_usage([], "today")
        assert result == {}
        d1_client.query.assert_not_awaited()

    async def test_get_batch_usage_chunked_across_two_queries(self, store, d1_client):
        """100 user IDs must be split into two queries (99 + 1) to stay within D1's param limit."""
        user_ids = [f"u{i}" for i in range(100)]

        first_chunk_rows = [{"user_id": f"u{i}", "cost": float(i)} for i in range(99)]
        last_chunk_rows = [{"user_id": "u99", "cost": 99.0}]
        d1_client.query.side_effect = [
            D1Result(rows=first_chunk_rows),
            D1Result(rows=last_chunk_rows),
        ]

        result = await store.get_batch_usage(user_ids, "today")

        assert d1_client.query.await_count == 2, "100 IDs must produce exactly 2 queries"

        first_call_params = d1_client.query.call_args_list[0][0][1]
        second_call_params = d1_client.query.call_args_list[1][0][1]
        # First query: 1 day filter + 99 user IDs = 100 params
        assert len(first_call_params) == 100
        # Second query: 1 day filter + 1 user ID = 2 params
        assert len(second_call_params) == 2

        assert len(result) == 100
        assert result["u0"] == 0.0
        assert result["u99"] == 99.0

    async def test_get_batch_usage_month_period(self, store, d1_client):
        """month period sends LIKE filter instead of exact day."""
        d1_client.query.return_value = D1Result(rows=[{"user_id": "u1", "cost": 10.0}])
        await store.get_batch_usage(["u1"], "month")
        sql, params = d1_client.query.call_args[0]
        assert "LIKE" in sql
        assert params[0].endswith("%")


class TestProtocol:
    """Verify D1OperationalStore satisfies the OperationalStore ABC."""

    def test_is_subclass(self):
        from serving.storage.base import OperationalStore

        assert issubclass(D1OperationalStore, OperationalStore)

    def test_instantiation_succeeds(self, d1_client):
        store = D1OperationalStore(d1_client)
        assert store is not None
