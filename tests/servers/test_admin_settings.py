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
    by_key = {s["key"]: s for s in data["settings"]}
    # int settings expose `min` so the admin UI can validate before submit;
    # bool settings have no bounds.
    assert by_key["user_concurrency_free"]["min"] == 1
    assert by_key["signup_enabled"]["min"] is None


@pytest.mark.asyncio
async def test_list_settings_excludes_routewise_keys(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.get(
        "/admin/settings",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    keys = {item["key"] for item in response.json()["settings"]}
    assert "routewise_budget_alpha" not in keys
    assert "routewise_latency_slo_sec" not in keys
    assert "routewise_latency_min_samples" not in keys


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


@pytest.mark.asyncio
async def test_update_int_setting_below_min_returns_400(monkeypatch, admin_client):
    """A numeric setting with a `min` floor rejects out-of-range values."""
    from serving.config import runtime_settings as rs_mod

    # Inject a temporary int setting with min=1 for the duration of the test.
    monkeypatch.setitem(
        rs_mod.RUNTIME_SETTINGS_REGISTRY,
        "_test_floored_int",
        {"type": "int", "default": 5, "min": 1, "description": "Test-only floored int"},
    )
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/_test_floored_int",
        json={"value": 0},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 400
    assert "min" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_int_setting_at_min_succeeds(monkeypatch, admin_client):
    """The boundary value is accepted."""
    from serving.config import runtime_settings as rs_mod

    monkeypatch.setitem(
        rs_mod.RUNTIME_SETTINGS_REGISTRY,
        "_test_floored_int",
        {"type": "int", "default": 5, "min": 1, "description": "Test-only floored int"},
    )
    client, op_store, _ = admin_client
    op_store.get_setting.return_value = None

    response = await client.patch(
        "/admin/settings/_test_floored_int",
        json={"value": 1},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    assert response.json()["value"] == 1


@pytest.mark.asyncio
async def test_list_settings_includes_user_concurrency_keys(admin_client):
    """All five user_concurrency_<role> keys are exposed via /admin/settings."""
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.get(
        "/admin/settings",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    keys = {item["key"] for item in response.json()["settings"]}
    assert {
        "user_concurrency_free",
        "user_concurrency_pro",
        "user_concurrency_internal",
        "user_concurrency_admin",
    }.issubset(keys)


@pytest.mark.asyncio
async def test_update_user_concurrency_admin_below_min_returns_400(admin_client):
    """Negative caps are rejected; the admin floor is 0 (= unlimited)."""
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/user_concurrency_admin",
        json={"value": -1},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_update_user_concurrency_admin_at_min_succeeds(admin_client):
    """0 is the 'unlimited' sentinel and is accepted for the admin cap."""
    client, op_store, _ = admin_client
    op_store.get_setting.return_value = None

    response = await client.patch(
        "/admin/settings/user_concurrency_admin",
        json={"value": 0},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    assert response.json()["value"] == 0


@pytest.mark.asyncio
async def test_update_int_setting_rejects_bool(admin_client):
    """JSON booleans must not be accepted for int settings (bool is a subclass
    of int in Python; without the explicit guard we'd persist ``"True"`` and
    later fail to coerce it back to int)."""
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/user_concurrency_free",
        json={"value": True},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 400
    assert "integer" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_float_setting_rejects_bool(monkeypatch, admin_client):
    """Same guard applies to float settings."""
    from serving.config import runtime_settings as rs_mod

    monkeypatch.setitem(
        rs_mod.RUNTIME_SETTINGS_REGISTRY,
        "_test_float_setting",
        {"type": "float", "default": 1.0, "description": "Test-only float"},
    )
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/_test_float_setting",
        json={"value": False},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 400
    assert "numeric" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_list_settings_includes_log_rejected_requests(admin_client):
    """The new log_rejected_requests bool setting is exposed via /admin/settings."""
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.get(
        "/admin/settings",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    by_key = {item["key"]: item for item in response.json()["settings"]}
    assert "log_rejected_requests" in by_key
    assert by_key["log_rejected_requests"]["value_type"] == "bool"
    assert by_key["log_rejected_requests"]["default_value"] is False


@pytest.mark.asyncio
async def test_list_settings_includes_log_synthetic_probes(admin_client):
    """The log_synthetic_probes bool setting is exposed via /admin/settings."""
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.get(
        "/admin/settings",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert response.status_code == 200
    by_key = {item["key"]: item for item in response.json()["settings"]}
    assert "log_synthetic_probes" in by_key
    assert by_key["log_synthetic_probes"]["value_type"] == "bool"
    assert by_key["log_synthetic_probes"]["default_value"] is False
