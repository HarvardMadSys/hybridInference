"""Tests for admin API key management endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router


@pytest.fixture
def mock_stores():
    """Create mock operational and log stores."""
    op_store = MagicMock()
    op_store.check_active_key_exists = AsyncMock(return_value=False)
    op_store.create_key = AsyncMock()
    op_store.list_keys = AsyncMock(return_value=(0, []))
    op_store.get_key_detail = AsyncMock()
    op_store.update_key = AsyncMock()
    op_store.revoke_key = AsyncMock()
    op_store.regenerate_key = AsyncMock()
    op_store.log_admin_action = AsyncMock()
    op_store.get_batch_usage = AsyncMock(return_value={})

    log_store = MagicMock()
    log_store.get_key_detail_usage = AsyncMock(return_value={})

    return op_store, log_store


@pytest.fixture
async def admin_client(monkeypatch, mock_stores):
    op_store, log_store = mock_stores
    app = FastAPI(title="Admin API Test")

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
    monkeypatch.setattr("serving.servers.routers.admin._admin_legacy.log_admin_action", mock_log_action)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")

    try:
        yield client, op_store, log_store, mock_log_action
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_create_api_key_success(admin_client, monkeypatch):
    client, op_store, _log_store, log_action = admin_client
    created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    op_store.check_active_key_exists.return_value = False
    op_store.create_key.return_value = {"id": 1, "created_at": created_at}

    monkeypatch.setattr("serving.servers.routers.admin._admin_legacy.generate_api_key", lambda: "hyi-fixed-key")
    monkeypatch.setattr("serving.servers.routers.admin._admin_legacy.hash_api_key", lambda _: "hashed-key")

    response = await client.post(
        "/admin/api-keys",
        headers={"Authorization": "Bearer test-admin"},
        json={"user_id": "alice", "quota_daily_cost_usd": 500},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["api_key"] == "hyi-fixed-key"
    assert body["key_prefix"] == "hyi-fixed-key"[:12]
    op_store.create_key.assert_awaited_once()
    log_action.assert_awaited()


@pytest.mark.asyncio
async def test_create_api_key_conflict(admin_client, monkeypatch):
    client, op_store, _log_store, log_action = admin_client
    op_store.check_active_key_exists.return_value = True

    response = await client.post(
        "/admin/api-keys",
        headers={"Authorization": "Bearer test-admin"},
        json={"user_id": "alice"},
    )

    assert response.status_code == 409
    log_action.assert_awaited()
    call = log_action.await_args
    assert call.kwargs.get("success") is False


@pytest.mark.asyncio
async def test_create_api_key_requires_token(admin_client):
    client, _op_store, _log_store, _log_action = admin_client
    response = await client.post("/admin/api-keys", json={"user_id": "alice"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_api_keys_batches_usage(admin_client):
    client, op_store, _log_store, _log_action = admin_client
    created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)

    op_store.list_keys.return_value = (
        1,
        [
            {
                "user_id": "alice",
                "user_name": "Alice",
                "key_prefix": "hyi-alice",
                "status": "active",
                "quota_daily_cost_usd": Decimal("500"),
                "quota_monthly_cost_usd": Decimal("10000"),
                "created_at": created_at,
                "last_used_at": None,
                "expires_at": None,
                "notes": None,
            }
        ],
    )
    op_store.get_batch_usage.side_effect = [
        {"alice": 12.34},  # today
        {"alice": 23.45},  # month
    ]

    response = await client.get(
        "/admin/api-keys",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["keys"]) == 1
    assert float(data["keys"][0]["usage_today_usd"]) == pytest.approx(12.34)
    assert float(data["keys"][0]["usage_month_usd"]) == pytest.approx(23.45)


@pytest.mark.asyncio
async def test_get_api_key_detail_success(admin_client):
    client, op_store, log_store, _log_action = admin_client
    created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)

    op_store.get_key_detail.return_value = {
        "user_id": "alice",
        "user_name": "Alice",
        "key_prefix": "hyi-alice",
        "status": "active",
        "quota_daily_cost_usd": Decimal("100"),
        "quota_monthly_cost_usd": Decimal("500"),
        "created_at": created_at,
        "last_used_at": None,
        "expires_at": None,
        "notes": None,
        "metadata": None,
    }
    log_store.get_key_detail_usage.return_value = {
        "today": {"cost_usd": 20.0, "requests": 5},
        "this_month": {"cost_usd": 50.0, "requests": 12},
        "models_used": ["model-a"],
        "last_request_at": created_at,
    }

    response = await client.get(
        "/admin/api-keys/alice",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == "alice"
    assert payload["usage"]["today"]["quota_remaining_usd"] == 80.0
    assert payload["usage"]["models_used"] == ["model-a"]


@pytest.mark.asyncio
async def test_update_api_key_success(admin_client):
    client, op_store, _log_store, log_action = admin_client
    op_store.get_key_detail.return_value = {"user_id": "alice"}

    response = await client.patch(
        "/admin/api-keys/alice",
        headers={"Authorization": "Bearer test-admin"},
        json={"quota_daily_cost_usd": 200},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["updated_fields"] == ["quota_daily_cost_usd"]
    log_action.assert_awaited()


@pytest.mark.asyncio
async def test_update_api_key_no_fields(admin_client):
    client, _op_store, _log_store, _log_action = admin_client
    response = await client.patch(
        "/admin/api-keys/alice",
        headers={"Authorization": "Bearer test-admin"},
        json={},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_api_key_not_found(admin_client):
    client, op_store, _log_store, _log_action = admin_client
    op_store.get_key_detail.return_value = None

    response = await client.patch(
        "/admin/api-keys/missing",
        headers={"Authorization": "Bearer test-admin"},
        json={"quota_daily_cost_usd": 100},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_revoke_api_key_soft_delete(admin_client):
    client, op_store, _log_store, log_action = admin_client
    op_store.get_key_detail.return_value = {"user_id": "alice"}

    response = await client.delete(
        "/admin/api-keys/alice",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.revoke_key.assert_awaited_once()
    log_action.assert_awaited()


@pytest.mark.asyncio
async def test_revoke_api_key_hard_delete(admin_client):
    client, op_store, _log_store, _log_action = admin_client
    op_store.get_key_detail.return_value = {"user_id": "alice"}

    response = await client.delete(
        "/admin/api-keys/alice",
        headers={"Authorization": "Bearer test-admin"},
        params={"hard_delete": "true"},
    )

    assert response.status_code == 200
    op_store.revoke_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_regenerate_api_key_success(admin_client, monkeypatch):
    client, op_store, _log_store, log_action = admin_client
    op_store.regenerate_key.return_value = "hyi-old"
    monkeypatch.setattr("serving.servers.routers.admin._admin_legacy.generate_api_key", lambda: "hyi-new-key")
    monkeypatch.setattr("serving.servers.routers.admin._admin_legacy.hash_api_key", lambda _: "new-hash")

    response = await client.post(
        "/admin/api-keys/alice/regenerate",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_key"] == "hyi-new-key"
    assert payload["old_key_prefix"] == "hyi-old"
    op_store.regenerate_key.assert_awaited_once()
    log_action.assert_awaited()
