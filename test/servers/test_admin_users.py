"""Tests for admin user management: search, audit log, and delete user endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


class _AcquireContext:
    """Async context manager for mocked pool.acquire()."""

    def __init__(self, connection: AsyncMock) -> None:
        self._connection = connection

    async def __aenter__(self) -> AsyncMock:
        return self._connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        return None


class _TransactionContext:
    """Async context manager for mocked conn.transaction()."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        return None


@pytest.fixture
def mocked_db_logger():
    logger = MagicMock()
    connection = AsyncMock()
    connection.fetch = AsyncMock()
    connection.fetchrow = AsyncMock()
    connection.execute = AsyncMock()
    connection.transaction = MagicMock(return_value=_TransactionContext())
    pool = MagicMock()
    pool.acquire.return_value = _AcquireContext(connection)
    logger.pool = pool
    return logger, connection


@pytest.fixture
async def admin_client(monkeypatch, mocked_db_logger):
    logger, connection = mocked_db_logger
    app = FastAPI(title="Admin Users Test")

    services = AppServices(
        router=MagicMock(),
        db_logger=logger,
        rate_limiter=None,
        routing_manager=None,
    )
    app.state.services = services  # type: ignore[attr-defined]
    app.include_router(admin_router.router)

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")

    mock_log_action = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.log_admin_action", mock_log_action)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")

    try:
        yield client, connection, mock_log_action
    finally:
        await client.aclose()


AUTH = {"Authorization": "Bearer test-admin"}

_NOW = datetime(2025, 6, 15, tzinfo=timezone.utc)


def _user_row(
    *,
    uid: str = "u1",
    email: str = "alice@example.com",
    status: str = "active",
    role: str = "free",
) -> dict[str, Any]:
    return {
        "id": uid,
        "email": email,
        "user_name": "Alice",
        "role": role,
        "status": status,
        "email_verified": True,
        "approval_note": None,
        "reviewed_at": None,
        "reviewed_by": None,
        "created_at": _NOW,
        "last_login_at": None,
        "key_prefix": "hyi-abc",
        "key_status": "active",
        "key_tier": "free",
    }


# ========================================================================
# Feature 1: User Search
# ========================================================================


@pytest.mark.asyncio
async def test_list_users_search(admin_client):
    """GET /admin/users?search=alice returns matching users."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 1}

    connection.fetch.reset_mock()
    connection.fetch.side_effect = [
        # status counts
        [{"status": "active", "cnt": 1}],
        # user rows
        [_user_row()],
        # usage today (empty)
        [],
        # usage month (empty)
        [],
    ]

    response = await client.get("/admin/users?search=alice", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["users"]) == 1
    assert data["users"][0]["email"] == "alice@example.com"

    # Verify the count query received the ILIKE param
    count_call = connection.fetchrow.await_args
    sql = count_call.args[0]
    assert "ILIKE" in sql


@pytest.mark.asyncio
async def test_list_users_search_combined_with_status(admin_client):
    """GET /admin/users?status=active&search=alice combines both filters."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 0}

    connection.fetch.reset_mock()
    connection.fetch.side_effect = [
        # status counts
        [{"status": "active", "cnt": 3}],
        # user rows (empty — no match)
        [],
    ]

    response = await client.get("/admin/users?status=active&search=nope", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert len(data["users"]) == 0

    # Verify both status and ILIKE appear in count SQL
    count_sql = connection.fetchrow.await_args.args[0]
    assert "status" in count_sql
    assert "ILIKE" in count_sql


@pytest.mark.asyncio
async def test_list_users_deleted_count(admin_client):
    """status_counts includes 'deleted' field."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 2}

    connection.fetch.reset_mock()
    connection.fetch.side_effect = [
        [{"status": "active", "cnt": 1}, {"status": "deleted", "cnt": 1}],
        [_user_row()],
        [],
        [],
    ]

    response = await client.get("/admin/users", headers=AUTH)

    assert response.status_code == 200
    counts = response.json()["status_counts"]
    assert counts["deleted"] == 1
    assert counts["active"] == 1


# ========================================================================
# Feature 2: Audit Log
# ========================================================================


def _audit_row(
    *,
    action: str = "approve_user",
    target: str = "u1",
    success: bool = True,
) -> dict[str, Any]:
    return {
        "id": 1,
        "timestamp": _NOW,
        "admin_ip": "admin@test",
        "action": action,
        "target_user_id": target,
        "details": '{"email": "alice@example.com"}',
        "success": success,
    }


@pytest.mark.asyncio
async def test_list_audit_log_success(admin_client):
    """GET /admin/audit-log returns entries."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 2}
    connection.fetch.reset_mock()
    connection.fetch.return_value = [
        _audit_row(action="approve_user"),
        _audit_row(action="reject_user"),
    ]

    response = await client.get("/admin/audit-log", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["entries"]) == 2
    assert data["entries"][0]["action"] == "approve_user"


@pytest.mark.asyncio
async def test_list_audit_log_filter_by_action(admin_client):
    """GET /admin/audit-log?action=approve_user filters correctly."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 1}
    connection.fetch.reset_mock()
    connection.fetch.return_value = [_audit_row(action="approve_user")]

    response = await client.get("/admin/audit-log?action=approve_user", headers=AUTH)

    assert response.status_code == 200
    # Verify the WHERE clause contains action filter
    count_sql = connection.fetchrow.await_args.args[0]
    assert "action" in count_sql
    assert connection.fetchrow.await_args.args[1] == "approve_user"


@pytest.mark.asyncio
async def test_list_audit_log_pagination(admin_client):
    """GET /admin/audit-log?limit=10&offset=20 passes pagination."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 50}
    connection.fetch.reset_mock()
    connection.fetch.return_value = []

    response = await client.get("/admin/audit-log?limit=10&offset=20", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 50

    # Verify limit/offset are in the query params
    fetch_sql = connection.fetch.await_args.args[0]
    assert "LIMIT" in fetch_sql
    assert "OFFSET" in fetch_sql


@pytest.mark.asyncio
async def test_list_audit_log_jsonb_string_parsing(admin_client):
    """Audit log handles JSONB returned as string (asyncpg edge case)."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 1}

    row = _audit_row()
    row["details"] = '{"key": "value"}'  # JSON string, not dict
    connection.fetch.reset_mock()
    connection.fetch.return_value = [row]

    response = await client.get("/admin/audit-log", headers=AUTH)

    assert response.status_code == 200
    entry = response.json()["entries"][0]
    assert entry["details"] == {"key": "value"}


@pytest.mark.asyncio
async def test_list_audit_log_requires_auth(admin_client):
    """GET /admin/audit-log without auth returns 401."""
    client, _connection, _log = admin_client
    response = await client.get("/admin/audit-log")
    assert response.status_code == 401


# ========================================================================
# Feature 3: Delete User
# ========================================================================


@pytest.mark.asyncio
async def test_delete_user_success(admin_client):
    """POST /admin/users/{id}/delete soft-deletes and cleans up."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.side_effect = [
        {"id": "u1", "email": "alice@example.com", "status": "active"},
    ]

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "Account requested deletion"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deleted"
    assert body["email"] == "alice@example.com"

    # Verify multi-table cleanup executed
    execute_calls = connection.execute.await_args_list
    sql_strs = [call.args[0] for call in execute_calls]

    # Should have: UPDATE users, UPDATE api_keys, DELETE sessions, DELETE email tokens,
    # DELETE password tokens, INSERT audit log
    assert any("UPDATE users SET status = 'deleted'" in s for s in sql_strs)
    assert any("api_keys" in s and "revoked" in s for s in sql_strs)
    assert any("auth_sessions" in s for s in sql_strs)
    assert any("email_verification_tokens" in s for s in sql_strs)
    assert any("password_reset_tokens" in s for s in sql_strs)
    assert any("admin_audit_log" in s for s in sql_strs)


@pytest.mark.asyncio
async def test_delete_user_from_suspended(admin_client):
    """Suspended users can be deleted."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.side_effect = [
        {"id": "u1", "email": "bob@example.com", "status": "suspended"},
    ]

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "Policy violation"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"


@pytest.mark.asyncio
async def test_delete_user_rejects_pending(admin_client):
    """Cannot delete a user with status 'pending_approval' — returns 409."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.side_effect = [
        {"id": "u1", "email": "carol@example.com", "status": "pending_approval"},
    ]

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_user_rejects_already_deleted(admin_client):
    """Cannot delete an already-deleted user — returns 409."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.side_effect = [
        {"id": "u1", "email": "dave@example.com", "status": "deleted"},
    ]

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_user_rejects_rejected(admin_client):
    """Cannot delete a user with status 'rejected' — returns 409."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.side_effect = [
        {"id": "u1", "email": "eve@example.com", "status": "rejected"},
    ]

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_user_not_found(admin_client):
    """Delete non-existent user returns 404."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.side_effect = [None]

    response = await client.post(
        "/admin/users/missing/delete",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_user_blank_reason_rejected(admin_client):
    """Whitespace-only reason is rejected with 422."""
    client, _connection, _log = admin_client

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "   "},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_user_empty_reason_rejected(admin_client):
    """Empty reason string is rejected with 422."""
    client, _connection, _log = admin_client

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": ""},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_user_requires_auth(admin_client):
    """POST /admin/users/{id}/delete without auth returns 401."""
    client, _connection, _log = admin_client
    response = await client.post(
        "/admin/users/u1/delete",
        json={"reason": "test"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_delete_user_key_cleanup_covers_legacy_user_id(admin_client):
    """Key revocation SQL uses (account_id = $1 OR user_id = $1)."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.side_effect = [
        {"id": "u1", "email": "alice@example.com", "status": "active"},
    ]

    await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "cleanup"},
    )

    execute_calls = connection.execute.await_args_list
    key_sql = [c.args[0] for c in execute_calls if "api_keys" in c.args[0]]
    assert len(key_sql) == 1
    assert "account_id" in key_sql[0]
    assert "user_id" in key_sql[0]
    assert "OR" in key_sql[0]


@pytest.mark.asyncio
async def test_delete_user_audit_inside_transaction(admin_client):
    """Audit log INSERT is inside the same transaction as the delete."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.side_effect = [
        {"id": "u1", "email": "alice@example.com", "status": "active"},
    ]

    await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "test atomicity"},
    )

    # conn.transaction() was called (wrapping all writes)
    connection.transaction.assert_called()

    # Audit INSERT was one of the execute calls inside the transaction
    execute_calls = connection.execute.await_args_list
    audit_calls = [c for c in execute_calls if "admin_audit_log" in c.args[0]]
    assert len(audit_calls) == 1
    audit_sql = audit_calls[0].args[0]
    assert "INSERT INTO admin_audit_log" in audit_sql


# ========================================================================
# PATCH /admin/users/{id} — delete bypass guard
# ========================================================================


@pytest.mark.asyncio
async def test_patch_user_rejects_deleted_status(admin_client):
    """PATCH /admin/users/{id} with status=deleted is rejected by schema."""
    client, _connection, _log = admin_client

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"status": "deleted"},
    )

    # Schema validation rejects 'deleted' (pattern only allows active|suspended)
    assert response.status_code == 422
