"""Tests for admin user management: search, audit log, and delete user endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
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
    op_store.list_user_activity_providers = AsyncMock(return_value=[])
    op_store.get_user_by_id = AsyncMock()
    op_store.approve_user = AsyncMock()
    op_store.reject_user = AsyncMock()
    op_store.delete_user = AsyncMock()
    op_store.resume_user = AsyncMock()
    op_store.begin_hard_delete_user = AsyncMock(return_value="claim-token")
    op_store.release_hard_delete_user_claim = AsyncMock()
    op_store.hard_delete_user = AsyncMock(return_value={})
    op_store.list_audit_log = AsyncMock(return_value=(0, []))
    op_store.log_admin_action = AsyncMock()
    op_store.update_user_fields = AsyncMock()
    op_store.set_user_preference = AsyncMock(return_value=True)
    op_store.revoke_key = AsyncMock()
    op_store.revoke_user_sessions = AsyncMock(return_value=0)
    op_store.suspend_user = AsyncMock()
    op_store.get_active_key_by_account = AsyncMock(return_value=None)

    log_store = MagicMock()
    log_store.get_batch_usage = AsyncMock(return_value={})
    log_store.hard_delete_user_data = AsyncMock(return_value={})
    log_store.account_has_erasure_fence = AsyncMock(return_value=False)

    return op_store, log_store


@pytest.fixture
async def admin_client(monkeypatch, mock_stores):
    op_store, log_store = mock_stores
    app = FastAPI(title="Admin Users Test")

    class _Adapter(BaseAdapter):
        async def chat_completion(self, messages, **params):
            return self.format_response(content="ok", model=self.config.id)

        async def stream_chat_completion(self, messages, **params):  # pragma: no cover
            if False:
                yield ""

    router = FixedRouter()
    for model_id in ("a-model", "z-model", "old-model"):
        router.register_route(
            model_id,
            [
                (
                    _Adapter(
                        ModelConfig(
                            id=model_id,
                            name=model_id,
                            provider="test",
                            base_url="http://test",
                            context_length=8192,
                            max_output_length=4096,
                            supported_params=["temperature"],
                            input_modalities=["text"],
                            output_modalities=["text"],
                            quantization="bf16",
                        )
                    ),
                    1.0,
                )
            ],
        )

    response_store = MagicMock()
    response_store.delete_user_responses = AsyncMock(return_value=0)
    batch_store = MagicMock()
    batch_store.delete_user_batches = AsyncMock(return_value=0)

    services = AppServices(
        router=router,
        db_logger=MagicMock(),
        operational_store=op_store,
        log_store=log_store,
        responses_store=response_store,
        batch_store=batch_store,
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
    monkeypatch.setenv("ERASURE_FENCE_PROTOCOL_READY", "true")

    try:
        yield client, op_store, log_store, mock_log_action
    finally:
        await client.aclose()


AUTH = {"Authorization": "Bearer test-admin"}

_NOW = datetime(2025, 6, 15, tzinfo=UTC)


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
        "signup_reason": None,
        "admin_note": None,
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
async def test_user_filter_providers_union_logged_and_routable(admin_client):
    """GET /admin/users/providers offers what the provider filter can match.

    A provider that only survives in api_logs — removed or renamed since —
    stays selectable, and a routed provider with no traffic yet is offered too.
    """
    client, op_store, _log_store, _log = admin_client
    op_store.list_user_activity_providers.return_value = ["retired-vendor", "test"]

    response = await client.get("/admin/users/providers", headers=AUTH)

    assert response.status_code == 200
    providers = {row["provider"]: row for row in response.json()["providers"]}
    assert providers["retired-vendor"] == {
        "provider": "retired-vendor",
        "display_name": "retired-vendor",
        "in_logs": True,
        "routable": False,
    }
    assert providers["test"]["in_logs"] is True
    assert providers["test"]["routable"] is True
    assert [row["provider"] for row in response.json()["providers"]] == sorted(providers)
    op_store.list_user_activity_providers.assert_awaited_once_with(days=30)


@pytest.mark.asyncio
async def test_user_filter_providers_requires_admin(admin_client):
    client, _op_store, _log_store, _log = admin_client
    response = await client.get("/admin/users/providers")
    assert response.status_code == 401


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


@pytest.mark.asyncio
async def test_patch_user_refuses_a_quota_for_a_keyless_user_before_writing(admin_client):
    """The quota lives on the key; refusing it must not leave the rest applied unaudited."""
    client, op_store, _log_store, mock_log = admin_client
    op_store.get_user_by_id.return_value = _user_row()
    op_store.get_active_key_by_account.return_value = None

    response = await client.patch(
        "/admin/users/u1", headers=AUTH, json={"role": "admin", "quota_daily_cost_usd": 5}
    )

    assert response.status_code == 409
    op_store.update_user_fields.assert_not_awaited()
    mock_log.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_user_refuses_a_quota_with_a_suspension_before_writing(admin_client):
    """Suspending revokes the key the quota would be written to."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row()
    op_store.get_active_key_by_account.return_value = {"key_prefix": "hyi-abc"}
    op_store.suspend_user = AsyncMock()

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"status": "suspended", "quota_daily_cost_usd": 5},
    )

    assert response.status_code == 409
    op_store.suspend_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_user_sets_admin_note(admin_client):
    """PATCH /admin/users/{id} persists a free-text admin note."""
    client, op_store, _log_store, mock_log = admin_client
    op_store.get_user_by_id.return_value = _user_row()

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"admin_note": "  VIP customer — handle with care  "},
    )

    assert response.status_code == 200
    # Stored trimmed.
    op_store.update_user_fields.assert_awaited_once_with(
        "u1", admin_note="VIP customer — handle with care"
    )
    assert response.json()["updated_fields"] == ["admin_note"]
    audit_payload = mock_log.await_args.args[4]
    assert audit_payload["values"]["admin_note"] == "VIP customer — handle with care"


@pytest.mark.asyncio
async def test_patch_user_clears_admin_note_with_blank(admin_client):
    """A blank/whitespace admin_note clears the note (stored as NULL)."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row()

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"admin_note": "   "},
    )

    assert response.status_code == 200
    op_store.update_user_fields.assert_awaited_once_with("u1", admin_note=None)


@pytest.mark.asyncio
async def test_patch_user_rejects_overlong_admin_note(admin_client):
    """admin_note longer than the schema limit is rejected (422)."""
    client, _op_store, _log_store, _log = admin_client

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"admin_note": "x" * 2001},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_user_detail_returns_admin_note(admin_client):
    """GET /admin/users/{id}/detail surfaces the stored admin note."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {**_user_row(), "admin_note": "watch this user"}
    op_store.get_active_key_by_account.return_value = None
    log_store.get_user_detail_usage = AsyncMock(
        return_value={"avg_turns": None, "avg_user_turns": None}
    )

    response = await client.get("/admin/users/u1/detail", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["admin_note"] == "watch this user"


@pytest.mark.asyncio
async def test_patch_user_sets_suspension_message(admin_client):
    """PATCH /admin/users/{id} persists a trimmed user-facing suspension message."""
    client, op_store, _log_store, mock_log = admin_client
    op_store.get_user_by_id.return_value = _user_row()

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"suspension_message": "  Contact support to appeal.  "},
    )

    assert response.status_code == 200
    op_store.update_user_fields.assert_awaited_once_with(
        "u1", suspension_message="Contact support to appeal."
    )
    assert response.json()["updated_fields"] == ["suspension_message"]
    audit_payload = mock_log.await_args.args[4]
    assert audit_payload["values"]["suspension_message"] == "Contact support to appeal."


@pytest.mark.asyncio
async def test_patch_user_clears_suspension_message_with_blank(admin_client):
    """A blank/whitespace suspension_message clears it (stored as NULL)."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row()

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"suspension_message": "   "},
    )

    assert response.status_code == 200
    op_store.update_user_fields.assert_awaited_once_with("u1", suspension_message=None)


@pytest.mark.asyncio
async def test_patch_user_rejects_overlong_suspension_message(admin_client):
    """suspension_message longer than the schema limit is rejected (422)."""
    client, _op_store, _log_store, _log = admin_client

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"suspension_message": "x" * 2001},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_user_detail_returns_suspension_message(admin_client):
    """GET /admin/users/{id}/detail surfaces the stored suspension message."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        **_user_row(),
        "suspension_message": "Suspended for review.",
    }
    op_store.get_active_key_by_account.return_value = None
    log_store.get_user_detail_usage = AsyncMock(
        return_value={"avg_turns": None, "avg_user_turns": None}
    )

    response = await client.get("/admin/users/u1/detail", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["suspension_message"] == "Suspended for review."


@pytest.mark.asyncio
async def test_get_user_detail_returns_disabled_models(admin_client):
    """GET /admin/users/{id}/detail exposes normalized disabled_models."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        **_user_row(),
        "preferences": {"disabled_models": ["z-model", "a-model", "a-model", 123]},
    }
    op_store.get_active_key_by_account.return_value = None
    # Detail usage is read whenever the log store exists (even without an active
    # key), so the turn averages stay consistent with the bulk list endpoint.
    log_store.get_user_detail_usage = AsyncMock(
        return_value={"avg_turns": None, "avg_user_turns": None}
    )

    response = await client.get("/admin/users/u1/detail", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["disabled_models"] == ["a-model", "z-model"]


@pytest.mark.asyncio
async def test_get_user_detail_returns_ask_question_fraction(admin_client):
    """GET /admin/users/{id}/detail surfaces the all-time ask-question fraction."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row()
    op_store.get_active_key_by_account.return_value = None
    log_store.get_user_detail_usage = AsyncMock(
        return_value={
            "avg_turns": None,
            "avg_user_turns": None,
            "ask_question_fraction": 0.5,
        }
    )

    response = await client.get("/admin/users/u1/detail", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["ask_question_fraction"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_get_user_detail_gates_activity_stats(admin_client):
    """Activity stats (turn averages, ask-question share) are computed only when
    ``include_activity_stats=true`` so the default row-expand stays fast."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row()
    op_store.get_active_key_by_account.return_value = None
    log_store.get_user_detail_usage = AsyncMock(
        return_value={
            "avg_turns": None,
            "avg_user_turns": None,
            "ask_question_fraction": None,
        }
    )

    # Default: the expensive full-history scan is skipped.
    response = await client.get("/admin/users/u1/detail", headers=AUTH)
    assert response.status_code == 200
    log_store.get_user_detail_usage.assert_awaited_once_with("u1", include_activity_stats=False)

    # Opt-in via query param forwards the flag to the store.
    log_store.get_user_detail_usage.reset_mock()
    response = await client.get("/admin/users/u1/detail?include_activity_stats=true", headers=AUTH)
    assert response.status_code == 200
    log_store.get_user_detail_usage.assert_awaited_once_with("u1", include_activity_stats=True)


@pytest.mark.asyncio
async def test_patch_user_updates_disabled_models(admin_client):
    """PATCH /admin/users/{id} stores normalized disabled_models in preferences."""
    client, op_store, _log_store, mock_log = admin_client
    op_store.get_user_by_id.return_value = {
        **_user_row(),
        "preferences": {"theme": "dark", "disabled_models": ["old-model"]},
    }
    op_store.get_user_preferences = AsyncMock(
        return_value={"theme": "dark", "disabled_models": ["old-model"]}
    )

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"disabled_models": ["z-model", "a-model", "a-model"]},
    )

    assert response.status_code == 200
    # Only the one key is written: the user's other preferences ("theme") are
    # not read back and rewritten, so a concurrent change to them survives.
    op_store.set_user_preference.assert_awaited_once_with(
        "u1", "disabled_models", ["a-model", "z-model"]
    )
    assert response.json()["updated_fields"] == ["disabled_models"]
    audit_payload = mock_log.await_args.args[4]
    assert audit_payload["values"]["disabled_models"] == ["a-model", "z-model"]


@pytest.mark.asyncio
async def test_patch_user_prunes_stale_disabled_models(admin_client):
    """PATCH /admin/users/{id} tolerates stale legacy ids already in preferences."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        **_user_row(),
        "preferences": {"disabled_models": ["old-retired-model"]},
    }
    op_store.get_user_preferences = AsyncMock(
        return_value={"disabled_models": ["old-retired-model"]}
    )

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"disabled_models": ["old-retired-model", "a-model"]},
    )

    assert response.status_code == 200
    op_store.set_user_preference.assert_awaited_once_with("u1", "disabled_models", ["a-model"])


@pytest.mark.asyncio
async def test_patch_user_drops_unknown_disabled_models(admin_client):
    """PATCH /admin/users/{id} silently drops unknown disabled model ids."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {**_user_row(), "preferences": {}}
    op_store.get_user_preferences = AsyncMock(return_value={})

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"disabled_models": ["definitely-unknown-model", "a-model"]},
    )

    assert response.status_code == 200
    op_store.set_user_preference.assert_awaited_once_with("u1", "disabled_models", ["a-model"])


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
    usage_alltime_requests: int = 0,
    usage_alltime_tokens: int = 0,
    key_prefix: str | None = "hyi-abc",
    last_login_at: datetime | None = None,
    signup_reason: str | None = None,
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
        "signup_reason": signup_reason,
        "created_at": _NOW,
        "last_login_at": last_login_at,
        "key_prefix": key_prefix,
        "key_status": "active" if key_prefix else None,
        "usage_today": usage_today,
        "usage_month": usage_month,
        "usage_alltime": usage_alltime,
        "usage_alltime_requests": usage_alltime_requests,
        "usage_alltime_tokens": usage_alltime_tokens,
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
async def test_signup_reason_surfaced_in_list(admin_client):
    """The signup reason is returned so admins can review pending users."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 1, "pending_approval": 1}
    reason = "Building a course assistant for CS50."
    op_store.list_users.return_value = (
        1,
        [_user_row_with_usage(status="pending_approval", signup_reason=reason)],
        sc,
    )

    response = await client.get("/admin/users", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert data["users"][0]["signup_reason"] == reason


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
async def test_sort_by_requests(admin_client):
    """sort_by=requests surfaces and orders by all-time request count."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 2, "active": 2}
    rows = [
        _user_row_with_usage(uid="u1", email="busy@e.com", usage_alltime_requests=4200),
        _user_row_with_usage(uid="u2", email="quiet@e.com", usage_alltime_requests=7),
    ]
    op_store.list_users.return_value = (2, rows, sc)

    response = await client.get("/admin/users?sort_by=requests", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert op_store.list_users.call_args.kwargs["sort_by"] == "requests"
    assert data["users"][0]["usage_alltime_requests"] == 4200
    assert data["users"][1]["usage_alltime_requests"] == 7


@pytest.mark.asyncio
async def test_sort_by_tokens(admin_client):
    """sort_by=tokens surfaces and orders by all-time total tokens."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 2, "active": 2}
    rows = [
        _user_row_with_usage(uid="u1", email="whale@e.com", usage_alltime_tokens=9_000_000),
        _user_row_with_usage(uid="u2", email="minnow@e.com", usage_alltime_tokens=512),
    ]
    op_store.list_users.return_value = (2, rows, sc)

    response = await client.get("/admin/users?sort_by=tokens", headers=AUTH)

    assert response.status_code == 200
    data = response.json()
    assert op_store.list_users.call_args.kwargs["sort_by"] == "tokens"
    assert data["users"][0]["usage_alltime_tokens"] == 9_000_000
    assert data["users"][1]["usage_alltime_tokens"] == 512


@pytest.mark.asyncio
async def test_usage_totals_returned_by_default(admin_client):
    """usage_alltime_requests/tokens are returned even without sorting by them."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 1, "active": 1}
    op_store.list_users.return_value = (
        1,
        [_user_row_with_usage(usage_alltime_requests=12, usage_alltime_tokens=3456)],
        sc,
    )

    response = await client.get("/admin/users", headers=AUTH)

    assert response.status_code == 200
    user = response.json()["users"][0]
    assert user["usage_alltime_requests"] == 12
    assert user["usage_alltime_tokens"] == 3456


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

    for sort_val in (
        "created",
        "cost_today",
        "cost_month",
        "cost_alltime",
        "last_login",
        "requests",
        "tokens",
    ):
        op_store.list_users.return_value = (0, [], _EMPTY_SC)
        response = await client.get(f"/admin/users?sort_by={sort_val}", headers=AUTH)
        assert response.status_code == 200, f"Failed for sort_by={sort_val}"


@pytest.mark.asyncio
async def test_alltime_usage_returned_without_cost_sort(admin_client):
    """usage_alltime_usd is returned even when not sorting by cost_alltime."""
    client, op_store, _log_store, _log = admin_client

    sc = {**_EMPTY_SC, "all": 1, "active": 1}
    op_store.list_users.return_value = (
        1,
        [_user_row_with_usage(usage_alltime=Decimal("12.34"))],
        sc,
    )

    response = await client.get("/admin/users", headers=AUTH)

    assert response.status_code == 200
    user = response.json()["users"][0]
    assert float(user["usage_alltime_usd"]) == 12.34


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
    # Resume goes through op_store.resume_user(), which includes the
    # fence check internally.
    op_store.resume_user.assert_awaited_once()
    call_kwargs = op_store.resume_user.await_args.kwargs
    assert call_kwargs["email"] == "alice@example.com"
    assert call_kwargs["reason"] == "false alarm"
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
    # Resume goes through op_store.resume_user().
    op_store.resume_user.assert_awaited_once()


# ========================================================================
# Feature 6: Hard Delete User
# ========================================================================


@pytest.mark.asyncio
async def test_resume_user_blocked_when_fenced(admin_client):
    """Resuming a hard-deleted (fenced) account returns 409."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }
    _log_store.account_has_erasure_fence.return_value = True

    response = await client.post(
        "/admin/users/u1/resume",
        headers=AUTH,
        json={"reason": "test"},
    )

    assert response.status_code == 409
    assert "permanently" in response.json()["detail"].lower()
    op_store.resume_user.assert_not_awaited()


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
    manager.attach_mock(op_store.begin_hard_delete_user, "op_store_begin_hard_delete")
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
    assert call_kwargs["claim_token"] == "claim-token"
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

    # Stored Responses API rows for the user are purged too.
    response_store = client._transport.app.state.services.responses_store
    response_store.delete_user_responses.assert_awaited_once_with("u1")

    # So are the user's batches, with their stored requests and results.
    batch_store = client._transport.app.state.services.batch_store
    batch_store.delete_user_batches.assert_awaited_once_with("u1")

    op_store.begin_hard_delete_user.assert_awaited_once_with("u1")

    # The operational claim precedes LogStore purge and final deletion.
    call_names = [c[0] for c in manager.mock_calls]
    claim_idx = call_names.index("op_store_begin_hard_delete")
    log_idx = call_names.index("log_store_hard_delete_data")
    op_store_idx = call_names.index("op_store_hard_delete")
    assert claim_idx < log_idx < op_store_idx, (
        f"log_store_hard_delete_data must run before op_store_hard_delete, got {call_names}"
    )


@pytest.mark.asyncio
async def test_hard_delete_aborts_when_resume_wins_race(admin_client):
    """Hard-delete aborts when a concurrent resume wins the race.

    The operational claim detects a concurrent resume before the LogStore
    purge and returns 409 without touching either downstream store.
    """
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }
    from serving.exceptions import HardDeleteStateChanged

    op_store.begin_hard_delete_user.side_effect = HardDeleteStateChanged("reactivated")

    response = await client.post(
        "/admin/users/u1/hard-delete",
        headers=AUTH,
        json={"confirm": True, "reason": "test"},
    )

    # Endpoint returns 409 (conflict) because state changed.
    assert response.status_code == 409
    assert "changed" in response.json()["detail"].lower()

    op_store.begin_hard_delete_user.assert_awaited_once_with("u1")
    log_store.hard_delete_user_data.assert_not_awaited()

    # But response_store, batch_store and op_store were NOT touched.
    response_store = client._transport.app.state.services.responses_store
    response_store.delete_user_responses.assert_not_awaited()
    batch_store = client._transport.app.state.services.batch_store
    batch_store.delete_user_batches.assert_not_awaited()
    op_store.hard_delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_delete_purges_batches_before_the_account_rows(admin_client):
    """Batches go after the log purge and before the operational wipe.

    A failed batch purge leaves the user soft-deleted with the fence in place,
    so the admin's retry purges what is left.
    """
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }
    batch_store = client._transport.app.state.services.batch_store
    manager = MagicMock()
    manager.attach_mock(log_store.hard_delete_user_data, "log_store_hard_delete_data")
    manager.attach_mock(batch_store.delete_user_batches, "batch_store_delete_user_batches")
    manager.attach_mock(op_store.hard_delete_user, "op_store_hard_delete")

    response = await client.post(
        "/admin/users/u1/hard-delete",
        headers=AUTH,
        json={"confirm": True},
    )

    assert response.status_code == 200
    call_names = [c[0] for c in manager.mock_calls]
    assert call_names == [
        "log_store_hard_delete_data",
        "batch_store_delete_user_batches",
        "op_store_hard_delete",
    ]


@pytest.mark.asyncio
async def test_hard_delete_stops_when_the_batch_purge_fails(admin_client):
    """The account rows stay, so the hard-delete can be retried."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }
    batch_store = client._transport.app.state.services.batch_store
    batch_store.delete_user_batches.side_effect = RuntimeError("batch store unavailable")

    with pytest.raises(RuntimeError, match="batch store unavailable"):
        await client.post(
            "/admin/users/u1/hard-delete",
            headers=AUTH,
            json={"confirm": True},
        )

    op_store.hard_delete_user.assert_not_awaited()
    # The log purge already established the fence, so the claim is kept.
    op_store.release_hard_delete_user_claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_delete_works_without_a_batch_store(admin_client):
    """A deployment without a database-backed batch store has nothing to purge."""
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }
    client._transport.app.state.services.batch_store = None

    response = await client.post(
        "/admin/users/u1/hard-delete",
        headers=AUTH,
        json={"confirm": True},
    )

    assert response.status_code == 200
    op_store.hard_delete_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_delete_purges_batches_while_their_migration_is_pending(admin_client):
    """A batch store still waiting for its schema migration at boot is purged all the same.

    Batches stored before this boot are in the database whether or not this
    process has finished migrating their tables.
    """
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }
    services = client._transport.app.state.services
    pending = services.batch_store
    services.batch_store = None
    services.batch_store_pending = pending

    response = await client.post(
        "/admin/users/u1/hard-delete",
        headers=AUTH,
        json={"confirm": True},
    )

    assert response.status_code == 200
    pending.delete_user_batches.assert_awaited_once_with("u1")


@pytest.mark.asyncio
async def test_hard_delete_releases_claim_when_log_purge_fails(admin_client):
    """A failed pre-fence purge must not strand the operational claim."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }
    log_store.hard_delete_user_data.side_effect = RuntimeError("log store unavailable")
    log_store.account_has_erasure_fence.return_value = False

    with pytest.raises(RuntimeError, match="log store unavailable"):
        await client.post(
            "/admin/users/u1/hard-delete",
            headers=AUTH,
            json={"confirm": True},
        )

    op_store.begin_hard_delete_user.assert_awaited_once_with("u1")
    log_store.hard_delete_user_data.assert_awaited_once_with("u1")
    log_store.account_has_erasure_fence.assert_awaited_once_with("u1")
    op_store.release_hard_delete_user_claim.assert_awaited_once_with("u1", "claim-token")
    op_store.hard_delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_delete_keeps_claim_when_fence_exists_after_failure(admin_client):
    """An ambiguous post-fence failure must retain the safety claim."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "alice@example.com",
        "status": "deleted",
    }
    log_store.hard_delete_user_data.side_effect = RuntimeError("ambiguous commit")
    log_store.account_has_erasure_fence.return_value = True

    with pytest.raises(RuntimeError, match="ambiguous commit"):
        await client.post(
            "/admin/users/u1/hard-delete",
            headers=AUTH,
            json={"confirm": True},
        )

    op_store.release_hard_delete_user_claim.assert_not_awaited()


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

    resp = await client.get("/admin/users/cost-history?user_ids=u1,u2&days=7", headers=AUTH)

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
async def test_get_bulk_ask_question_fractions_route(admin_client):
    """GET /admin/users/ask-question-fractions returns one entry per user."""
    client, _op_store, log_store, _log = admin_client
    log_store.get_bulk_user_ask_question_fractions = AsyncMock(
        return_value={
            "u1": {"ask_question_fraction": 0.25, "n_requests": 8, "n_ask_requests": 2},
            "u2": {"ask_question_fraction": None, "n_requests": 0, "n_ask_requests": 0},
        }
    )

    resp = await client.get("/admin/users/ask-question-fractions?user_ids=u1,u2", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["fractions"].keys()) == {"u1", "u2"}
    assert body["fractions"]["u1"]["ask_question_fraction"] == pytest.approx(0.25)
    assert body["fractions"]["u1"]["n_ask_requests"] == 2
    assert body["fractions"]["u2"]["ask_question_fraction"] is None


@pytest.mark.asyncio
async def test_get_bulk_ask_question_fractions_empty_ids(admin_client):
    """Empty user_ids returns empty fractions without hitting the store."""
    client, _op_store, log_store, _log = admin_client
    log_store.get_bulk_user_ask_question_fractions = AsyncMock(return_value={})
    resp = await client.get("/admin/users/ask-question-fractions?user_ids=", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["fractions"] == {}
    log_store.get_bulk_user_ask_question_fractions.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_bulk_ask_question_fractions_too_many_ids(admin_client):
    """More than 200 user_ids returns 422."""
    client, _op_store, log_store, _log = admin_client
    log_store.get_bulk_user_ask_question_fractions = AsyncMock(return_value={})
    ids = ",".join(f"u{i}" for i in range(201))
    resp = await client.get(f"/admin/users/ask-question-fractions?user_ids={ids}", headers=AUTH)
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
async def test_list_users_passes_new_filters_through(admin_client):
    """GET /admin/users?<new filters> forwards them to op_store.list_users."""
    client, op_store, _log_store, _log = admin_client
    op_store.list_users = AsyncMock(return_value=(0, [], {}))
    op_store.list_user_activity_providers = AsyncMock(return_value=[])

    resp = await client.get(
        "/admin/users?min_cost_today=5&quota_state=near&provider=anthropic&active_within_hours=24",
        headers=AUTH,
    )
    assert resp.status_code == 200

    op_store.list_users.assert_awaited_once()
    kwargs = op_store.list_users.await_args.kwargs
    assert kwargs["min_cost_today"] == Decimal("5")
    assert kwargs["quota_state"] == "near"
    assert kwargs["provider"] == "anthropic"
    assert kwargs["active_within_hours"] == 24


@pytest.mark.asyncio
async def test_list_users_min_cost_month_passes_through(admin_client):
    """min_cost_month forwarded as Decimal."""
    client, op_store, _log_store, _log = admin_client
    op_store.list_users = AsyncMock(return_value=(0, [], {}))
    op_store.list_user_activity_providers = AsyncMock(return_value=[])
    resp = await client.get("/admin/users?min_cost_month=10.5", headers=AUTH)
    assert resp.status_code == 200
    kwargs = op_store.list_users.await_args.kwargs
    assert kwargs["min_cost_month"] == Decimal("10.5")


@pytest.mark.asyncio
async def test_list_users_quota_state_invalid_returns_422(admin_client):
    """Unknown quota_state returns 422."""
    client, op_store, _log_store, _log = admin_client
    op_store.list_users = AsyncMock(return_value=(0, [], {}))
    op_store.list_user_activity_providers = AsyncMock(return_value=[])
    resp = await client.get("/admin/users?quota_state=bogus", headers=AUTH)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_users_anomaly_param_passes_through(admin_client):
    """GET /admin/users?anomaly=true forwards anomaly=True to op_store.list_users."""
    client, op_store, _log_store, _log = admin_client
    op_store.list_users = AsyncMock(return_value=(0, [], {}))
    op_store.list_user_activity_providers = AsyncMock(return_value=[])

    resp = await client.get("/admin/users?anomaly=true", headers=AUTH)
    assert resp.status_code == 200

    op_store.list_users.assert_awaited_once()
    kwargs = op_store.list_users.await_args.kwargs
    assert kwargs["anomaly"] is True


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


@pytest.mark.asyncio
async def test_lightweight_users_preserves_null_totals(admin_client):
    client, op_store, _, _ = admin_client
    row = _user_row()
    row.update(usage_alltime_requests=None, usage_alltime_tokens=None)
    op_store.list_users.return_value = (1, [row], _EMPTY_SC)
    response = await client.get("/admin/users?include_usage_totals=false", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["users"][0]["usage_alltime_requests"] is None
    assert response.json()["users"][0]["usage_alltime_tokens"] is None
    assert op_store.list_users.call_args.kwargs["include_usage_totals"] is False


@pytest.mark.asyncio
async def test_bulk_usage_totals_requires_admin_and_bounds_request(admin_client):
    client, op_store, _, _ = admin_client
    op_store.get_bulk_user_usage_totals = AsyncMock(
        return_value={"u1": {"requests": 3, "tokens": 5}}
    )
    response = await client.get("/admin/users/usage-totals?user_ids=u1", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["totals"]["u1"] == {"requests": 3, "tokens": 5}
    response = await client.get("/admin/users/usage-totals?user_ids=u1")
    assert response.status_code in (401, 403)
    response = await client.get(
        "/admin/users/usage-totals",
        params={"user_ids": ",".join(str(i) for i in range(201))},
        headers=AUTH,
    )
    assert response.status_code == 422
    response = await client.get("/admin/users/usage-totals", headers=AUTH)
    assert response.json() == {"totals": {}}
    op_store.get_bulk_user_usage_totals.assert_awaited_once_with(["u1"])


# ========================================================================
# Pending signups, account status and quota: the routes' own decisions
# (end to end against PostgreSQL: test_admin_user_lifecycle_postgres.py)
# ========================================================================


@pytest.fixture
def sent_emails(monkeypatch):
    """Record the approval/rejection mails instead of sending them."""
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr("serving.utils.email.is_email_enabled", lambda: True)
    monkeypatch.setattr(
        "serving.utils.email.send_approval_email", lambda to: sent.append(("approve", to))
    )
    monkeypatch.setattr(
        "serving.utils.email.send_rejection_email",
        lambda to, reason: sent.append(("reject", to, reason)),
    )
    return sent


@pytest.mark.asyncio
async def test_reject_a_pending_user_records_and_tells_them_why(admin_client, sent_emails):
    client, op_store, _log_store, log_action = admin_client
    op_store.get_user_by_id.return_value = _user_row(status="pending_approval")

    resp = await client.post(
        "/admin/users/u1/reject", headers=AUTH, json={"reason": "outside the pilot"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    op_store.reject_user.assert_awaited_once()
    assert op_store.reject_user.await_args.kwargs["reason"] == "outside the pilot"
    assert log_action.await_args.args[2:4] == ("reject_user", "u1")
    assert sent_emails == [("reject", "alice@example.com", "outside the pilot")]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("status", ["active", "suspended", "rejected", "deleted"])
async def test_only_a_pending_user_can_be_decided(admin_client, sent_emails, decision, status):
    client, op_store, _log_store, log_action = admin_client
    op_store.get_user_by_id.return_value = _user_row(status=status)

    resp = await client.post(
        f"/admin/users/u1/{decision}", headers=AUTH, json={"reason": "r", "note": "n"}
    )

    assert resp.status_code == 409
    assert status in resp.json()["detail"]
    op_store.approve_user.assert_not_awaited()
    op_store.reject_user.assert_not_awaited()
    log_action.assert_not_awaited()
    assert sent_emails == []


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_deciding_an_unknown_user_is_404(admin_client, decision):
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = None

    resp = await client.post(f"/admin/users/ghost/{decision}", headers=AUTH, json={"reason": "r"})

    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", "x" * 501])
async def test_a_rejection_needs_a_reason_the_user_can_read(admin_client, reason):
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row(status="pending_approval")

    resp = await client.post("/admin/users/u1/reject", headers=AUTH, json={"reason": reason})

    assert resp.status_code == 422
    op_store.reject_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        ("active", "suspended", True),
        ("suspended", "active", True),
        ("pending_approval", "active", False),  # approve/reject decide a signup
        ("rejected", "active", False),
        ("deleted", "active", False),  # resume decides a deletion
        ("active", "active", False),
    ],
)
async def test_status_moves_only_between_active_and_suspended(
    admin_client, current, target, allowed
):
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row(status=current)

    resp = await client.patch("/admin/users/u1", headers=AUTH, json={"status": target})

    assert resp.status_code == (200 if allowed else 409), resp.text
    # Only a suspension takes the key, and every signed-in session, away -- with
    # the status, in the store's one transaction rather than three writes.
    if allowed and target == "suspended":
        op_store.suspend_user.assert_awaited_once_with("u1")
        op_store.update_user_fields.assert_not_awaited()
    elif allowed:
        op_store.update_user_fields.assert_awaited_once_with("u1", status=target)
        op_store.suspend_user.assert_not_awaited()
    else:
        op_store.update_user_fields.assert_not_awaited()
        op_store.suspend_user.assert_not_awaited()
    op_store.revoke_key.assert_not_awaited()
    op_store.revoke_user_sessions.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_suspension_carries_its_other_fields_into_the_same_write(admin_client):
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row(status="active")

    response = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={"status": "suspended", "suspension_message": "  abuse  ", "admin_note": "ticket 9"},
    )

    assert response.status_code == 200
    op_store.suspend_user.assert_awaited_once_with(
        "u1", suspension_message="abuse", admin_note="ticket 9"
    )
    op_store.update_user_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_empty_patch_is_refused(admin_client):
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row()

    resp = await client.patch("/admin/users/u1", headers=AUTH, json={})

    assert resp.status_code == 422
    op_store.update_user_fields.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_quota_needs_a_live_key_and_is_written_to_it(admin_client):
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row()
    op_store.update_key = AsyncMock()

    op_store.get_active_key_by_account.return_value = None
    resp = await client.patch("/admin/users/u1", headers=AUTH, json={"quota_daily_cost_usd": "5"})
    assert resp.status_code == 409
    op_store.update_key.assert_not_awaited()

    op_store.get_active_key_by_account.return_value = {"id": 1}
    resp = await client.patch(
        "/admin/users/u1",
        headers=AUTH,
        json={
            "quota_daily_cost_usd": "5",
            "quota_monthly_cost_usd": "50",
            "max_concurrent_requests": 3,
        },
    )
    assert resp.status_code == 200, resp.text
    op_store.update_key.assert_awaited_once_with(
        "u1", quota_daily_cost_usd=Decimal("5"), quota_monthly_cost_usd=Decimal("50")
    )
    op_store.update_user_fields.assert_awaited_with("u1", max_concurrent_requests=3)
    assert set(resp.json()["updated_fields"]) == {
        "quota_daily_cost_usd",
        "quota_monthly_cost_usd",
        "max_concurrent_requests",
    }


@pytest.mark.asyncio
async def test_hard_delete_waits_for_the_fence_rollout(admin_client, monkeypatch):
    client, op_store, _log_store, _log = admin_client
    monkeypatch.setenv("ERASURE_FENCE_PROTOCOL_READY", "false")
    op_store.get_user_by_id.return_value = _user_row(status="deleted")

    resp = await client.post("/admin/users/u1/hard-delete", headers=AUTH, json={"confirm": True})

    assert resp.status_code == 503
    op_store.begin_hard_delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_delete_keeps_the_claim_when_the_fence_cannot_be_checked(admin_client):
    """Fail closed: not knowing whether the account is erased means keep it locked."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row(status="deleted")
    log_store.hard_delete_user_data.side_effect = RuntimeError("purge failed")
    log_store.account_has_erasure_fence.side_effect = RuntimeError("database gone")

    with pytest.raises(RuntimeError, match="purge failed"):
        await client.post("/admin/users/u1/hard-delete", headers=AUTH, json={"confirm": True})

    op_store.release_hard_delete_user_claim.assert_not_awaited()
    op_store.hard_delete_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_delete_reports_the_first_failure_when_the_release_also_fails(admin_client):
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row(status="deleted")
    log_store.hard_delete_user_data.side_effect = RuntimeError("purge failed")
    log_store.account_has_erasure_fence.return_value = False
    op_store.release_hard_delete_user_claim.side_effect = RuntimeError("release failed")

    with pytest.raises(RuntimeError, match="purge failed"):
        await client.post("/admin/users/u1/hard-delete", headers=AUTH, json={"confirm": True})

    op_store.release_hard_delete_user_claim.assert_awaited_once_with("u1", "claim-token")


# ========================================================================
# Automation scores and turn averages
# ========================================================================


def _score_record(uid: str = "u1", *, days: int = 30, score: float = 0.9) -> dict[str, Any]:
    return {
        "user_id": uid,
        "days": days,
        "score": score,
        "confidence": 0.8,
        "band": "likely_automated",
        "insufficient_data": False,
        "n_req": 120,
        "agent_share": 0.1,
        "signals": {
            "hour_spread": {"sub": 0.95, "weight": 0.4, "available": True},
            "ua": {"sub": None, "weight": 0.2, "available": False},
        },
        "detail": {"hours_active": 24},
    }


@pytest.mark.asyncio
async def test_bulk_automation_scores_are_returned_per_user(admin_client):
    client, _op_store, log_store, _log = admin_client
    log_store.get_bulk_user_automation_scores = AsyncMock(
        return_value={"u1": _score_record("u1", days=7), "u2": _score_record("u2", days=7)}
    )

    resp = await client.get(
        "/admin/users/automation-scores?user_ids= u1 ,u2,,&days=7", headers=AUTH
    )

    assert resp.status_code == 200
    log_store.get_bulk_user_automation_scores.assert_awaited_once_with(["u1", "u2"], days=7)
    body = resp.json()
    assert body["days"] == 7
    assert set(body["scores"]) == {"u1", "u2"}
    assert body["scores"]["u1"]["signals"]["hour_spread"] == {
        "sub": 0.95,
        "weight": 0.4,
        "available": True,
    }
    assert body["scores"]["u1"]["signals"]["ua"]["sub"] is None
    assert body["scores"]["u1"]["detail"] == {"hours_active": 24}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "store_method"),
    [
        ("/admin/users/automation-scores", "get_bulk_user_automation_scores"),
        ("/admin/users/turn-averages", "get_bulk_user_turn_averages"),
    ],
)
async def test_bulk_user_analytics_bound_the_id_list(admin_client, path, store_method):
    """An empty list costs no query; more than 200 ids is refused before one."""
    client, _op_store, log_store, _log = admin_client
    setattr(log_store, store_method, AsyncMock(return_value={}))

    empty = await client.get(f"{path}?user_ids=,%20,", headers=AUTH)
    too_many = await client.get(
        f"{path}?user_ids={','.join(f'u{i}' for i in range(201))}", headers=AUTH
    )
    at_limit = await client.get(
        f"{path}?user_ids={','.join(f'u{i}' for i in range(200))}", headers=AUTH
    )

    assert empty.status_code == 200
    assert too_many.status_code == 422
    assert too_many.json()["detail"] == "Maximum 200 user_ids per request"
    assert at_limit.status_code == 200
    assert getattr(log_store, store_method).await_count == 1  # only the 200-id call


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, 91])
@pytest.mark.parametrize(
    "path",
    ["/admin/users/automation-scores?user_ids=u1", "/admin/users/u1/automation-score?x=1"],
)
async def test_automation_score_window_is_one_to_ninety_days(admin_client, path, days):
    client, _op_store, log_store, _log = admin_client
    log_store.get_bulk_user_automation_scores = AsyncMock(return_value={})
    log_store.get_user_automation_score = AsyncMock(return_value=None)

    resp = await client.get(f"{path}&days={days}", headers=AUTH)

    assert resp.status_code == 422
    assert resp.json()["detail"] == "days must be between 1 and 90"
    log_store.get_bulk_user_automation_scores.assert_not_awaited()
    log_store.get_user_automation_score.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_user_with_no_traffic_gets_a_neutral_score_not_a_404(admin_client):
    client, _op_store, log_store, _log = admin_client
    log_store.get_user_automation_score = AsyncMock(return_value=None)

    resp = await client.get("/admin/users/u9/automation-score?days=14", headers=AUTH)

    assert resp.status_code == 200
    log_store.get_user_automation_score.assert_awaited_once_with("u9", days=14)
    assert resp.json() == {
        "user_id": "u9",
        "days": 14,
        "score": 0.5,
        "confidence": 0.0,
        "band": "mixed_or_uncertain",
        "insufficient_data": True,
        "n_req": 0,
        "agent_share": 0.0,
        "signals": {},
        "detail": {},
    }


@pytest.mark.asyncio
async def test_a_user_automation_score_is_returned_with_its_signals(admin_client):
    client, _op_store, log_store, _log = admin_client
    log_store.get_user_automation_score = AsyncMock(return_value=_score_record("u1"))

    resp = await client.get("/admin/users/u1/automation-score", headers=AUTH)

    assert resp.status_code == 200
    log_store.get_user_automation_score.assert_awaited_once_with("u1", days=30)
    assert resp.json()["score"] == pytest.approx(0.9)
    assert resp.json()["signals"]["ua"]["available"] is False


@pytest.mark.asyncio
async def test_bulk_turn_averages_are_returned_per_user(admin_client):
    client, _op_store, log_store, _log = admin_client
    log_store.get_bulk_user_turn_averages = AsyncMock(
        return_value={"u1": {"avg_turns": 6.5, "avg_user_turns": 3.0}, "u2": {}}
    )

    resp = await client.get("/admin/users/turn-averages?user_ids=u1,u2", headers=AUTH)

    assert resp.status_code == 200
    log_store.get_bulk_user_turn_averages.assert_awaited_once_with(["u1", "u2"])
    assert resp.json()["averages"] == {
        "u1": {"avg_turns": 6.5, "avg_user_turns": 3.0},
        "u2": {"avg_turns": None, "avg_user_turns": None},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/admin/users/automation-scores?user_ids=u1",
        "/admin/users/u1/automation-score",
        "/admin/users/turn-averages?user_ids=u1",
    ],
)
async def test_user_analytics_are_admin_only(admin_client, path):
    client, _op_store, log_store, _log = admin_client
    log_store.get_bulk_user_automation_scores = AsyncMock(return_value={})
    log_store.get_user_automation_score = AsyncMock(return_value=None)
    log_store.get_bulk_user_turn_averages = AsyncMock(return_value={})

    anonymous = await client.get(path)
    wrong = await client.get(path, headers={"Authorization": "Bearer not-the-admin"})

    assert anonymous.status_code == 401
    assert wrong.status_code in (401, 403)
    log_store.get_bulk_user_automation_scores.assert_not_awaited()
    log_store.get_user_automation_score.assert_not_awaited()
    log_store.get_bulk_user_turn_averages.assert_not_awaited()


# ========================================================================
# User detail usage, approval email, audit details, resume race
# ========================================================================


@pytest.mark.asyncio
async def test_user_detail_shows_usage_only_while_the_user_holds_a_key(admin_client):
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row()
    log_store.get_user_detail_usage = AsyncMock(
        return_value={
            "usage_today_usd": 1.25,
            "usage_today_requests": 4,
            "usage_month_usd": 9.5,
            "usage_month_requests": 40,
            "models_used": ["a-model"],
            "last_request_at": "2025-06-15T00:00:00+00:00",
            "avg_turns": 5.0,
        }
    )

    op_store.get_active_key_by_account.return_value = {
        "key_prefix": "hyi-abc",
        "quota_daily_cost_usd": Decimal("2"),
        "quota_monthly_cost_usd": None,
    }
    with_key = (await client.get("/admin/users/u1/detail", headers=AUTH)).json()

    op_store.get_active_key_by_account.return_value = None
    without_key = (await client.get("/admin/users/u1/detail", headers=AUTH)).json()

    assert with_key["has_key"] is True
    assert with_key["key_prefix"] == "hyi-abc"
    assert (with_key["quota_daily_usd"], with_key["quota_monthly_usd"]) == (2.0, None)
    assert with_key["usage_today_usd"] == pytest.approx(1.25)
    assert with_key["usage_month_requests"] == 40
    assert with_key["models_used"] == ["a-model"]
    assert with_key["last_request_at"] is not None

    assert without_key["has_key"] is False
    assert without_key["usage_month_usd"] == 0
    assert without_key["usage_today_requests"] == 0
    assert without_key["models_used"] == []
    assert without_key["last_request_at"] is None
    # Turn averages come from api_logs by user_id, so they stay visible.
    assert without_key["avg_turns"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_user_detail_shows_a_zero_quota_as_zero(admin_client):
    """0 is a limit (no spend allowed), and every request is refused under it."""
    client, op_store, log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row()
    log_store.get_user_detail_usage = AsyncMock(return_value={})
    op_store.get_active_key_by_account.return_value = {
        "key_prefix": "hyi-abc",
        "quota_daily_cost_usd": Decimal("0.0000"),
        "quota_monthly_cost_usd": Decimal("0.0000"),
    }

    detail = (await client.get("/admin/users/u1/detail", headers=AUTH)).json()

    assert (detail["quota_daily_usd"], detail["quota_monthly_usd"]) == (0.0, 0.0)


@pytest.mark.asyncio
async def test_user_detail_for_an_unknown_user_is_a_404(admin_client):
    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = None

    resp = await client.get("/admin/users/ghost/detail", headers=AUTH)

    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "email", "sent"),
    [
        (True, "alice@example.com", ["alice@example.com"]),
        (False, "alice@example.com", []),
        (True, None, []),  # a phone-only account has nowhere to send it
    ],
    ids=["sent", "email-off", "no-address"],
)
async def test_approval_emails_the_user_when_it_can(
    admin_client, monkeypatch, enabled, email, sent
):
    client, op_store, _log_store, _log = admin_client
    row = _user_row(status="pending_approval", email=email)
    row["phone"] = "+15550100" if email is None else None
    op_store.get_user_by_id.return_value = row
    delivered: list[str] = []
    monkeypatch.setattr("serving.utils.email.is_email_enabled", lambda: enabled)
    monkeypatch.setattr("serving.utils.email.send_approval_email", delivered.append)

    resp = await client.post("/admin/users/u1/approve", json={}, headers=AUTH)

    assert resp.status_code == 200
    assert delivered == sent


@pytest.mark.asyncio
async def test_audit_details_that_are_not_json_are_kept_raw(admin_client):
    client, op_store, _log_store, _log = admin_client
    row = _audit_row()
    row["details"] = "legacy free text"
    op_store.list_audit_log.return_value = (1, [row])

    resp = await client.get("/admin/audit-log", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["entries"][0]["details"] == {"raw": "legacy free text"}


@pytest.mark.asyncio
async def test_resume_loses_to_a_hard_delete_in_progress(admin_client):
    from serving.exceptions import HardDeleteStateChanged

    client, op_store, _log_store, _log = admin_client
    op_store.get_user_by_id.return_value = _user_row(status="deleted")
    op_store.resume_user.side_effect = HardDeleteStateChanged("claimed")

    resp = await client.post("/admin/users/u1/resume", json={}, headers=AUTH)

    assert resp.status_code == 409
    assert "hard-delete in progress" in resp.json()["detail"]
