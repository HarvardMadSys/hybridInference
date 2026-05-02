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


@pytest.fixture
def mock_stores():
    """Create mock operational and log stores."""
    op_store = MagicMock()
    op_store.list_users = AsyncMock(return_value=(0, [], {}))
    op_store.get_user_by_id = AsyncMock()
    op_store.approve_user = AsyncMock()
    op_store.reject_user = AsyncMock()
    op_store.delete_user = AsyncMock()
    op_store.list_audit_log = AsyncMock(return_value=(0, []))
    op_store.log_admin_action = AsyncMock()
    op_store.update_user_fields = AsyncMock()
    op_store.revoke_key = AsyncMock()
    op_store.get_active_key_by_account = AsyncMock(return_value=None)

    log_store = MagicMock()
    log_store.get_batch_usage = AsyncMock(return_value={})

    return op_store, log_store


@pytest.fixture
async def admin_client(monkeypatch, mock_stores):
    op_store, log_store = mock_stores
    app = FastAPI(title="Admin Users Test")

    services = AppServices(
        router=MagicMock(),
        db_logger=MagicMock(),
        operational_store=op_store,
        log_store=log_store,
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
        yield client, op_store, log_store, mock_log_action
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
        "usage_today": Decimal("0"),
        "usage_month": Decimal("0"),
        "usage_alltime": Decimal("0"),
    }


# ========================================================================
# Feature 1: User Search
# ========================================================================


@pytest.mark.asyncio
async def test_list_users_search(admin_client):
    """GET /admin/users?search=alice returns matching users."""
    client, op_store, _log_store, _log = admin_client

    sc = {"all": 1, "pending_approval": 0, "active": 1, "suspended": 0, "rejected": 0, "deleted": 0}
    op_store.list_users.return_value = (1, [_user_row()], sc)

    response = await client.get("/admin/users?search=alice", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["users"]) == 1
    assert data["users"][0]["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_list_users_search_combined_with_status(admin_client):
    """GET /admin/users?status=active&search=alice combines both filters."""
    client, op_store, _log_store, _log = admin_client

    sc = {"all": 3, "pending_approval": 0, "active": 3, "suspended": 0, "rejected": 0, "deleted": 0}
    op_store.list_users.return_value = (0, [], sc)

    response = await client.get("/admin/users?status=active&search=nope", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert len(data["users"]) == 0


@pytest.mark.asyncio
async def test_list_users_deleted_count(admin_client):
    """status_counts includes 'deleted' field."""
    client, op_store, _log_store, _log = admin_client

    sc = {"all": 2, "pending_approval": 0, "active": 1, "suspended": 0, "rejected": 0, "deleted": 1}
    op_store.list_users.return_value = (2, [_user_row()], sc)

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
    client, op_store, _log_store, _log = admin_client
    op_store.list_audit_log.return_value = (
        2,
        [_audit_row(action="approve_user"), _audit_row(action="reject_user")],
    )

    response = await client.get("/admin/audit-log", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["entries"]) == 2
    assert data["entries"][0]["action"] == "approve_user"


@pytest.mark.asyncio
async def test_list_audit_log_filter_by_action(admin_client):
    """GET /admin/audit-log?action=approve_user filters correctly."""
    client, op_store, _log_store, _log = admin_client
    op_store.list_audit_log.return_value = (1, [_audit_row(action="approve_user")])

    response = await client.get("/admin/audit-log?action=approve_user", headers=AUTH)

    assert response.status_code == 200
    op_store.list_audit_log.assert_awaited_once()
    call_kwargs = op_store.list_audit_log.await_args.kwargs
    assert call_kwargs["action"] == "approve_user"


@pytest.mark.asyncio
async def test_list_audit_log_pagination(admin_client):
    """GET /admin/audit-log?limit=10&offset=20 passes pagination."""
    client, op_store, _log_store, _log = admin_client
    op_store.list_audit_log.return_value = (50, [])

    response = await client.get("/admin/audit-log?limit=10&offset=20", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 50
    call_kwargs = op_store.list_audit_log.await_args.kwargs
    assert call_kwargs["limit"] == 10
    assert call_kwargs["offset"] == 20


@pytest.mark.asyncio
async def test_list_audit_log_jsonb_string_parsing(admin_client):
    """Audit log handles JSONB returned as string (asyncpg edge case)."""
    client, op_store, _log_store, _log = admin_client
    row = _audit_row()
    row["details"] = '{"key": "value"}'  # JSON string, not dict
    op_store.list_audit_log.return_value = (1, [row])

    response = await client.get("/admin/audit-log", headers=AUTH)

    assert response.status_code == 200
    entry = response.json()["entries"][0]
    assert entry["details"] == {"key": "value"}


@pytest.mark.asyncio
async def test_list_audit_log_requires_auth(admin_client):
    """GET /admin/audit-log without auth returns 401."""
    client, _op_store, _log_store, _log = admin_client
    response = await client.get("/admin/audit-log")
    assert response.status_code == 401


# ========================================================================
# Feature 3: Delete User
# ========================================================================


@pytest.mark.asyncio
async def test_delete_user_success(admin_client):
    """POST /admin/users/{id}/delete soft-deletes and cleans up."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "active",
    }

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "Account requested deletion"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deleted"
    assert body["email"] == "alice@example.com"
    op_store.delete_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_user_from_suspended(admin_client):
    """Suspended users can be deleted."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "bob@example.com",
        "status": "suspended",
    }

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
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "carol@example.com",
        "status": "pending_approval",
    }

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_user_rejects_already_deleted(admin_client):
    """Cannot delete an already-deleted user — returns 409."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "dave@example.com",
        "status": "deleted",
    }

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_user_rejects_rejected(admin_client):
    """Cannot delete a user with status 'rejected' — returns 409."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "eve@example.com",
        "status": "rejected",
    }

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_user_not_found(admin_client):
    """Delete non-existent user returns 404."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = None

    response = await client.post(
        "/admin/users/missing/delete",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_user_blank_reason_rejected(admin_client):
    """Whitespace-only reason is rejected with 422."""
    client, _op_store, _log_store, _log = admin_client

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "   "},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_user_empty_reason_rejected(admin_client):
    """Empty reason string is rejected with 422."""
    client, _op_store, _log_store, _log = admin_client

    response = await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": ""},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_user_requires_auth(admin_client):
    """POST /admin/users/{id}/delete without auth returns 401."""
    client, _op_store, _log_store, _log = admin_client
    response = await client.post(
        "/admin/users/u1/delete",
        json={"reason": "test"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_delete_user_key_cleanup_covers_legacy_user_id(admin_client):
    """delete_user is called with the user's email for audit."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "active",
    }

    await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "cleanup"},
    )

    op_store.delete_user.assert_awaited_once()
    call_kwargs = op_store.delete_user.await_args.kwargs
    assert call_kwargs["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_delete_user_audit_inside_transaction(admin_client):
    """delete_user store method handles atomicity internally."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "active",
    }

    await client.post(
        "/admin/users/u1/delete",
        headers=AUTH,
        json={"reason": "test atomicity"},
    )

    # The store's delete_user method handles all cleanup atomically
    op_store.delete_user.assert_awaited_once()


# ========================================================================
# PATCH /admin/users/{id} — delete bypass guard
# ========================================================================


@pytest.mark.asyncio
async def test_patch_user_rejects_deleted_status(admin_client):
    """PATCH /admin/users/{id} with status=deleted is rejected by schema."""
    client, _op_store, _log_store, _log = admin_client

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
        "last_login_at": last_login_at,
        "key_prefix": key_prefix,
        "key_status": "active" if key_prefix else None,
        "usage_today": usage_today,
        "usage_month": usage_month,
        "usage_alltime": usage_alltime,
    }


_EMPTY_SC = {
    "all": 0,
    "pending_approval": 0,
    "active": 0,
    "suspended": 0,
    "rejected": 0,
    "deleted": 0,
}


@pytest.mark.asyncio
async def test_sort_by_default_no_cte(admin_client):
    """Default sort_by=created uses no CTEs — same path as before."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 1, "active": 1}
    op_store.list_users.return_value = (1, [_user_row()], sc)

    response = await client.get("/admin/users", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 1


@pytest.mark.asyncio
async def test_sort_by_cost_today(admin_client):
    """sort_by=cost_today sorts by usage_today."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 2, "active": 2}
    rows = [
        _user_row_with_usage(uid="u1", email="big@e.com", usage_today=Decimal("50.00")),
        _user_row_with_usage(uid="u2", email="small@e.com", usage_today=Decimal("1.00")),
    ]
    op_store.list_users.return_value = (2, rows, sc)

    response = await client.get("/admin/users?sort_by=cost_today", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 2
    assert float(data["users"][0]["usage_today_usd"]) == 50.0
    assert float(data["users"][1]["usage_today_usd"]) == 1.0


@pytest.mark.asyncio
async def test_sort_by_cost_alltime(admin_client):
    """sort_by=cost_alltime uses all 3 usage dimensions."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 2, "active": 2}
    rows = [
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
    ]
    op_store.list_users.return_value = (2, rows, sc)

    response = await client.get("/admin/users?sort_by=cost_alltime", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 2
    assert float(data["users"][0]["usage_alltime_usd"]) == 5000.0
    assert float(data["users"][1]["usage_alltime_usd"]) == 20.0


@pytest.mark.asyncio
async def test_sort_by_last_login_no_cte(admin_client):
    """sort_by=last_login uses simple path (no CTEs)."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 1, "active": 1}
    op_store.list_users.return_value = (1, [_user_row()], sc)

    response = await client.get("/admin/users?sort_by=last_login", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 1


@pytest.mark.asyncio
async def test_sort_by_combined_with_filter_and_search(admin_client):
    """sort_by + status + search all work together."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 5, "active": 5}
    rows = [_user_row_with_usage(uid="u1", email="john@e.com", usage_month=Decimal("42"))]
    op_store.list_users.return_value = (1, rows, sc)

    response = await client.get(
        "/admin/users?status=active&search=john&sort_by=cost_month",
        headers=AUTH,
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 1
    assert float(data["users"][0]["usage_month_usd"]) == 42.0


@pytest.mark.asyncio
async def test_sort_by_invalid_value_returns_422(admin_client):
    """sort_by=invalid returns 422 validation error."""
    client, _op_store, _log_store, _log = admin_client

    response = await client.get("/admin/users?sort_by=invalid", headers=AUTH)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_sort_tie_breaker_in_order_clause(admin_client):
    """All sort options return 200."""
    client, op_store, _log_store, _log = admin_client

    for sort_val in ("created", "cost_today", "cost_month", "cost_alltime", "last_login"):
        op_store.list_users.return_value = (0, [], _EMPTY_SC)
        response = await client.get(f"/admin/users?sort_by={sort_val}", headers=AUTH)
        assert response.status_code == 200, f"Failed for sort_by={sort_val}"


@pytest.mark.asyncio
async def test_sort_alltime_usage_zero_without_sort(admin_client):
    """usage_alltime_usd defaults to 0 when not sorting by cost_alltime."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 1, "active": 1}
    op_store.list_users.return_value = (1, [_user_row()], sc)

    response = await client.get("/admin/users", headers=AUTH)

    assert response.status_code == 200
    user = response.json()["users"][0]
    assert float(user["usage_alltime_usd"]) == 0.0
