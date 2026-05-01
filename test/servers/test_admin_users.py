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


# ========================================================================
# Feature 4: Sort by Usage
# ========================================================================


def _user_row_with_usage(
    *,
    uid: str = "u1",
    email: str = "alice@example.com",
    status: str = "active",
    role: str = "free",
    usage_today: Decimal = Decimal("0"),
    usage_month: Decimal = Decimal("0"),
    usage_alltime: Decimal = Decimal("0"),
    key_prefix: str | None = "hyi-abc",
    last_login_at: datetime | None = None,
) -> dict[str, Any]:
    """User row with optional CTE usage columns."""
    row: dict[str, Any] = {
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
        "last_login_at": last_login_at,
        "key_prefix": key_prefix,
        "key_status": "active" if key_prefix else None,
        "key_tier": "free" if key_prefix else None,
    }
    # CTE columns (only present in CTE path)
    if usage_today is not None:
        row["usage_today"] = usage_today
    if usage_month is not None:
        row["usage_month"] = usage_month
    if usage_alltime is not None:
        row["usage_alltime"] = usage_alltime
    return row


@pytest.mark.asyncio
async def test_sort_by_default_no_cte(admin_client):
    """Default sort_by=created uses no CTEs — same path as before."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 1}

    connection.fetch.reset_mock()
    connection.fetch.side_effect = [
        [{"status": "active", "cnt": 1}],
        [_user_row()],
        [],  # usage today batch
        [],  # usage month batch
    ]

    response = await client.get("/admin/users", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 1

    # Verify the query uses ORDER BY created_at (simple path, no WITH)
    fetch_calls = connection.fetch.await_args_list
    user_query_sql = fetch_calls[1].args[0]  # 2nd fetch = user rows
    assert "WITH" not in user_query_sql
    assert "created_at DESC" in user_query_sql


@pytest.mark.asyncio
async def test_sort_by_cost_today(admin_client):
    """sort_by=cost_today uses CTE path with usage_today."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 2}

    connection.fetch.reset_mock()
    connection.fetch.side_effect = [
        [{"status": "active", "cnt": 2}],
        # CTE query returns user rows with usage_today column
        [
            _user_row_with_usage(uid="u1", email="big@e.com", usage_today=Decimal("50.00")),
            _user_row_with_usage(uid="u2", email="small@e.com", usage_today=Decimal("1.00")),
        ],
        # Batch: usage_month (today was in CTE, month still batch-fetched)
        [],
    ]

    response = await client.get("/admin/users?sort_by=cost_today", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 2
    # First user should have higher usage
    assert float(data["users"][0]["usage_today_usd"]) == 50.0
    assert float(data["users"][1]["usage_today_usd"]) == 1.0

    # Verify CTE SQL structure
    fetch_calls = connection.fetch.await_args_list
    cte_sql = fetch_calls[1].args[0]
    assert "WITH" in cte_sql
    assert "filtered_users" in cte_sql
    assert "usage_today" in cte_sql
    assert "COALESCE(ut.cost, 0) DESC" in cte_sql


@pytest.mark.asyncio
async def test_sort_by_cost_alltime(admin_client):
    """sort_by=cost_alltime uses all 3 CTEs, no batch queries needed."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 2}

    connection.fetch.reset_mock()
    connection.fetch.side_effect = [
        [{"status": "active", "cnt": 2}],
        # CTE query returns all 3 usage columns
        [
            _user_row_with_usage(
                uid="u1",
                email="whale@e.com",
                usage_today=Decimal("10"),
                usage_month=Decimal("100"),
                usage_alltime=Decimal("5000"),
            ),
            _user_row_with_usage(
                uid="u2",
                email="small@e.com",
                usage_today=Decimal("1"),
                usage_month=Decimal("5"),
                usage_alltime=Decimal("20"),
            ),
        ],
        # No batch queries — all dimensions in CTEs
    ]

    response = await client.get("/admin/users?sort_by=cost_alltime", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 2
    assert float(data["users"][0]["usage_alltime_usd"]) == 5000.0
    assert float(data["users"][1]["usage_alltime_usd"]) == 20.0

    # Verify all 3 CTEs present
    cte_sql = connection.fetch.await_args_list[1].args[0]
    assert "usage_today" in cte_sql
    assert "usage_month" in cte_sql
    assert "usage_alltime" in cte_sql
    assert "COALESCE(ua.cost, 0) DESC" in cte_sql

    # Only 2 fetch calls: status counts + CTE query (no batch)
    assert len(connection.fetch.await_args_list) == 2


@pytest.mark.asyncio
async def test_sort_by_last_login_no_cte(admin_client):
    """sort_by=last_login uses simple path (no CTEs)."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 1}

    connection.fetch.reset_mock()
    connection.fetch.side_effect = [
        [{"status": "active", "cnt": 1}],
        [_user_row()],
        [],  # usage today batch
        [],  # usage month batch
    ]

    response = await client.get("/admin/users?sort_by=last_login", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 1

    user_query_sql = connection.fetch.await_args_list[1].args[0]
    assert "WITH" not in user_query_sql
    assert "last_login_at DESC NULLS LAST" in user_query_sql


@pytest.mark.asyncio
async def test_sort_by_combined_with_filter_and_search(admin_client):
    """sort_by + status + search all work together."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 1}

    connection.fetch.reset_mock()
    connection.fetch.side_effect = [
        [{"status": "active", "cnt": 5}],
        [_user_row_with_usage(uid="u1", email="john@e.com", usage_month=Decimal("42"))],
        [],  # batch: usage_today
    ]

    response = await client.get(
        "/admin/users?status=active&search=john&sort_by=cost_month",
        headers=AUTH,
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 1
    assert float(data["users"][0]["usage_month_usd"]) == 42.0

    # Verify the CTE SQL has both filter predicates
    cte_sql = connection.fetch.await_args_list[1].args[0]
    assert "u.status" in cte_sql
    assert "ILIKE" in cte_sql
    assert "COALESCE(um.cost, 0) DESC" in cte_sql


@pytest.mark.asyncio
async def test_sort_by_invalid_value_returns_422(admin_client):
    """sort_by=invalid returns 422 validation error."""
    client, _connection, _log = admin_client

    response = await client.get("/admin/users?sort_by=invalid", headers=AUTH)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_sort_tie_breaker_in_order_clause(admin_client):
    """All sort options include tie-breaker columns for pagination stability."""
    client, connection, _log = admin_client

    for sort_val, expect_primary in [
        ("created", "created_at DESC"),
        ("cost_today", "COALESCE(ut.cost, 0) DESC"),
        ("cost_month", "COALESCE(um.cost, 0) DESC"),
        ("cost_alltime", "COALESCE(ua.cost, 0) DESC"),
        ("last_login", "last_login_at DESC NULLS LAST"),
    ]:
        connection.fetchrow.reset_mock()
        connection.fetchrow.return_value = {"total": 0}
        connection.fetch.reset_mock()
        connection.fetch.side_effect = [
            [{"status": "active", "cnt": 0}],
            [],  # empty result
        ]

        response = await client.get(f"/admin/users?sort_by={sort_val}", headers=AUTH)
        assert response.status_code == 200, f"Failed for sort_by={sort_val}"
        query_sql = connection.fetch.await_args_list[1].args[0]
        assert expect_primary in query_sql


@pytest.mark.asyncio
async def test_sort_alltime_usage_zero_without_sort(admin_client):
    """usage_alltime_usd defaults to 0 when not sorting by cost_alltime."""
    client, connection, _log = admin_client
    connection.fetchrow.reset_mock()
    connection.fetchrow.return_value = {"total": 1}

    connection.fetch.reset_mock()
    connection.fetch.side_effect = [
        [{"status": "active", "cnt": 1}],
        [_user_row()],
        [],  # usage today
        [],  # usage month
    ]

    response = await client.get("/admin/users", headers=AUTH)

    assert response.status_code == 200
    user = response.json()["users"][0]
    assert float(user["usage_alltime_usd"]) == 0.0
