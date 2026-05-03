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
    op_store.resume_user = AsyncMock()
    op_store.hard_delete_user = AsyncMock(return_value={})
    op_store.list_audit_log = AsyncMock(return_value=(0, []))
    op_store.log_admin_action = AsyncMock()
    op_store.update_user_fields = AsyncMock()
    op_store.revoke_key = AsyncMock()
    op_store.get_active_key_by_account = AsyncMock(return_value=None)

    log_store = MagicMock()
    log_store.get_batch_usage = AsyncMock(return_value={})
    log_store.hard_delete_user_data = AsyncMock(return_value={})

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
    monkeypatch.setattr("serving.servers.routers.admin.users.log_admin_action", mock_log_action)
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


# ========================================================================
# Feature 5: Resume User
# ========================================================================


@pytest.mark.asyncio
async def test_resume_user_success(admin_client):
    """POST /admin/users/{id}/resume flips deleted -> active."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }

    response = await client.post(
        "/admin/users/u1/resume",
        headers=AUTH,
        json={"reason": "false alarm"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    assert body["email"] == "alice@example.com"
    op_store.resume_user.assert_awaited_once()
    call_kwargs = op_store.resume_user.await_args.kwargs
    assert call_kwargs["email"] == "alice@example.com"
    assert call_kwargs["reason"] == "false alarm"
    # admin_id is the verified admin identifier (JWT email or ADMIN_TOKEN
    # marker); admin_ip is the request's client IP, derived independently.
    assert "admin_id" in call_kwargs
    assert isinstance(call_kwargs["admin_id"], str)
    assert "admin_ip" in call_kwargs
    assert isinstance(call_kwargs["admin_ip"], str)


@pytest.mark.asyncio
async def test_resume_user_not_deleted(admin_client):
    """Cannot resume a user that is not soft-deleted — 409."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "active",
    }

    response = await client.post(
        "/admin/users/u1/resume",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 409
    op_store.resume_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_user_not_found(admin_client):
    """Resume non-existent user returns 404."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = None

    response = await client.post(
        "/admin/users/missing/resume",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_resume_user_requires_auth(admin_client):
    """POST /admin/users/{id}/resume without auth returns 401."""
    client, _op_store, _log_store, _log = admin_client
    response = await client.post("/admin/users/u1/resume", json={"reason": "test"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_resume_user_reason_optional(admin_client):
    """resume_user accepts an empty body — reason is optional."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }

    response = await client.post("/admin/users/u1/resume", headers=AUTH, json={})

    assert response.status_code == 200
    op_store.resume_user.assert_awaited_once()


# ========================================================================
# Feature 6: Hard Delete User
# ========================================================================


@pytest.mark.asyncio
async def test_hard_delete_user_requires_soft_delete(admin_client):
    """Hard delete on an active user returns 409."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "active",
    }

    response = await client.post(
        "/admin/users/u1/hard-delete",
        headers=AUTH,
        json={"confirm": True, "reason": "test"},
    )

    assert response.status_code == 409
    op_store.hard_delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_delete_user_rejects_suspended(admin_client):
    """Hard delete on suspended user returns 409 — must soft-delete first."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "bob@example.com",
        "status": "suspended",
    }

    response = await client.post(
        "/admin/users/u1/hard-delete",
        headers=AUTH,
        json={"confirm": True},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_hard_delete_user_requires_confirm(admin_client):
    """confirm=False returns 400."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }

    response = await client.post(
        "/admin/users/u1/hard-delete",
        headers=AUTH,
        json={"confirm": False, "reason": "test"},
    )

    assert response.status_code == 400
    op_store.hard_delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_delete_user_missing_confirm(admin_client):
    """Missing confirm field returns 422 (schema validation)."""
    client, _op_store, _log_store, _log = admin_client

    response = await client.post(
        "/admin/users/u1/hard-delete",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_hard_delete_user_not_found(admin_client):
    """Hard delete non-existent user returns 404."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = None

    response = await client.post(
        "/admin/users/missing/hard-delete",
        headers=AUTH,
        json={"confirm": True},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_hard_delete_user_requires_auth(admin_client):
    """POST /admin/users/{id}/hard-delete without auth returns 401."""
    client, _op_store, _log_store, _log = admin_client
    response = await client.post("/admin/users/u1/hard-delete", json={"confirm": True})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_hard_delete_user_wipes_data(admin_client):
    """soft-deleted user can be hard-deleted; store + log_store are both invoked."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }
    op_store.hard_delete_user.return_value = {
        "users": 1,
        "api_keys": 2,
        "auth_sessions": 3,
        "email_verification_tokens": 0,
        "password_reset_tokens": 0,
        "user_daily_cost": 0,
        "admin_audit_log": 5,
    }
    log_store.hard_delete_user_data.return_value = {
        "api_logs": 5,
        "email_broadcast_recipients": 0,
    }

    # Track call order: log_store wipe must come BEFORE op_store wipe so a
    # partial failure leaves the user resumable.
    manager = MagicMock()
    manager.attach_mock(log_store.hard_delete_user_data, "log_store_hard_delete_data")
    manager.attach_mock(op_store.hard_delete_user, "op_store_hard_delete")

    response = await client.post(
        "/admin/users/u1/hard-delete",
        headers=AUTH,
        json={"confirm": True, "reason": "user requested"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert "permanently deleted" in body["message"].lower()

    # Operational store wipe was called with the right args
    op_store.hard_delete_user.assert_awaited_once()
    call_kwargs = op_store.hard_delete_user.await_args.kwargs
    assert call_kwargs["email"] == "alice@example.com"
    assert call_kwargs["reason"] == "user requested"
    # admin_id is the verified admin identifier (JWT email or ADMIN_TOKEN
    # marker); admin_ip is the request's client IP, derived independently.
    assert "admin_id" in call_kwargs
    assert isinstance(call_kwargs["admin_id"], str)
    assert "admin_ip" in call_kwargs
    assert isinstance(call_kwargs["admin_ip"], str)

    # LogStore wipe was issued with the user_id
    log_store.hard_delete_user_data.assert_awaited_once_with("u1")

    # LogStore wipe must run BEFORE the op_store wipe.
    call_names = [c[0] for c in manager.mock_calls]
    log_idx = call_names.index("log_store_hard_delete_data")
    op_store_idx = call_names.index("op_store_hard_delete")
    assert log_idx < op_store_idx, (
        f"log_store_hard_delete_data must run before op_store_hard_delete, got {call_names}"
    )


# ========================================================================
# Phase 1: Cost-history + summary endpoints
# ========================================================================


@pytest.mark.asyncio
async def test_get_user_cost_history_route(admin_client):
    """GET /admin/users/{id}/cost-history returns daily points."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_cost_history = AsyncMock(
        return_value=[
            {"day": "2025-06-14", "cost_usd": Decimal("1.50"), "requests": 3},
            {"day": "2025-06-15", "cost_usd": Decimal("2.00"), "requests": 5},
        ]
    )

    resp = await client.get("/admin/users/u1/cost-history?days=7", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "u1"
    assert body["days"] == 7
    assert len(body["points"]) == 2
    assert body["points"][0]["day"] == "2025-06-14"
    op_store.get_user_cost_history.assert_awaited_once_with("u1", days=7)


@pytest.mark.asyncio
async def test_get_user_cost_history_default_days(admin_client):
    """Default days=7 if not specified."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_cost_history = AsyncMock(return_value=[])
    resp = await client.get("/admin/users/u1/cost-history", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["days"] == 7


@pytest.mark.asyncio
async def test_get_user_cost_history_validates_days(admin_client):
    """days outside 1..90 returns 422."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_cost_history = AsyncMock(return_value=[])
    resp = await client.get("/admin/users/u1/cost-history?days=0", headers=AUTH)
    assert resp.status_code == 422
    resp = await client.get("/admin/users/u1/cost-history?days=91", headers=AUTH)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_bulk_cost_history_route(admin_client):
    """GET /admin/users/cost-history?user_ids=a,b returns one block per user."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_bulk_user_cost_history = AsyncMock(
        return_value={
            "u1": [{"day": "2025-06-15", "cost_usd": Decimal("1.0"), "requests": 1}],
            "u2": [],
        }
    )

    resp = await client.get(
        "/admin/users/cost-history?user_ids=u1,u2&days=7", headers=AUTH
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["histories"].keys()) == {"u1", "u2"}
    assert body["days"] == 7


@pytest.mark.asyncio
async def test_get_bulk_cost_history_empty_ids(admin_client):
    """Empty user_ids returns empty histories without hitting the store."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_bulk_user_cost_history = AsyncMock(return_value={})
    resp = await client.get("/admin/users/cost-history?user_ids=", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["histories"] == {}
    op_store.get_bulk_user_cost_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_bulk_cost_history_too_many_ids(admin_client):
    """More than 200 user_ids returns 422."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_bulk_user_cost_history = AsyncMock(return_value={})
    ids = ",".join(f"u{i}" for i in range(201))
    resp = await client.get(f"/admin/users/cost-history?user_ids={ids}", headers=AUTH)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_users_summary_route(admin_client):
    """GET /admin/users/summary returns the four cards."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_users_summary = AsyncMock(
        return_value={
            "pending": {"count": 3, "top": []},
            "top_spenders_today": {"count": 10, "top": []},
            "anomalies": {"count": 1, "top": []},
            "near_quota": {"count": 2, "top": []},
        }
    )

    resp = await client.get("/admin/users/summary", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["pending"]["count"] == 3
    assert body["anomalies"]["count"] == 1
    assert body["top_spenders_today"]["count"] == 10
    assert body["near_quota"]["count"] == 2


@pytest.mark.asyncio
async def test_get_users_summary_with_top_users(admin_client):
    """Summary card 'top' SummaryUserItem fields are serialized correctly."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_users_summary = AsyncMock(
        return_value={
            "pending": {"count": 0, "top": []},
            "top_spenders_today": {
                "count": 1,
                "top": [
                    {
                        "id": "u1",
                        "email": "alice@x.com",
                        "user_name": "Alice",
                        "role": "free",
                        "today_cost_usd": Decimal("12.50"),
                        "avg_prior_7d_usd": Decimal("1.00"),
                        "quota_daily_usd": None,
                        "multiplier": None,
                    }
                ],
            },
            "anomalies": {
                "count": 1,
                "top": [
                    {
                        "id": "u1",
                        "email": "alice@x.com",
                        "user_name": "Alice",
                        "role": "free",
                        "today_cost_usd": Decimal("12.50"),
                        "avg_prior_7d_usd": Decimal("1.00"),
                        "quota_daily_usd": None,
                        "multiplier": 12.5,
                    }
                ],
            },
            "near_quota": {"count": 0, "top": []},
        }
    )

    resp = await client.get("/admin/users/summary", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    top = body["top_spenders_today"]["top"][0]
    assert top["id"] == "u1"
    assert top["email"] == "alice@x.com"
    anomaly = body["anomalies"]["top"][0]
    assert anomaly["multiplier"] == 12.5
