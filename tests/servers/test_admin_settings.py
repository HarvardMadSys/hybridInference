"""Tests for admin runtime settings endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.runtime_settings import RuntimeSettings
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router


@pytest.fixture
def mock_stores():
    op_store = MagicMock()
    op_store.get_setting = AsyncMock(return_value=None)
    op_store.set_setting = AsyncMock()
    op_store.list_settings = AsyncMock(return_value=[])
    op_store.log_admin_action = AsyncMock()
    op_store.list_signup_allowed_domains = AsyncMock(return_value=[])
    return op_store


@pytest.fixture
async def admin_client(monkeypatch, mock_stores):
    op_store = mock_stores
    app = FastAPI(title="Admin Settings Test")

    services = AppServices(
        router=MagicMock(),
        db_logger=MagicMock(),
        operational_store=op_store,
        log_store=MagicMock(),
        runtime_settings=RuntimeSettings(op_store),
    )
    app.state.services = services
    app.include_router(admin_router.router)

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")

    mock_log_action = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.settings.log_admin_action", mock_log_action)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")

    try:
        yield client, op_store, mock_log_action
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_list_settings(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.get(
        "/admin/settings",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "settings" in data
    assert len(data["settings"]) > 0
    assert all("key" in s for s in data["settings"])
    assert all("value_type" in s for s in data["settings"])


@pytest.mark.asyncio
async def test_update_bool_setting(admin_client):
    client, op_store, log_action = admin_client
    op_store.get_setting.return_value = None

    response = await client.patch(
        "/admin/settings/signup_enabled",
        json={"value": False},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["key"] == "signup_enabled"
    assert data["value"] is False
    op_store.set_setting.assert_awaited_once()
    call = op_store.set_setting.call_args
    assert call[0][0] == "signup_enabled"
    assert call[0][1] == "False"
    assert call[0][2] == "bool"
    log_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_unknown_setting_returns_404(admin_client):
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/nonexistent_key",
        json={"value": True},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_wrong_type_returns_400(admin_client):
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/signup_enabled",
        json={"value": "not_a_bool"},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_update_requires_admin_auth(admin_client):
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/signup_enabled",
        json={"value": False},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_requires_admin_auth(admin_client):
    client, _, _ = admin_client

    response = await client.get("/admin/settings")
    assert response.status_code == 401
