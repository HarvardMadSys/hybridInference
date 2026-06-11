"""Tests for dedicated admin Routewise settings endpoints."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.model_router_registry import ModelRouterRegistry
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
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
    app = FastAPI(title="Admin Routewise Settings Test")

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
    monkeypatch.setattr("serving.servers.routers.admin.routewise.log_admin_action", mock_log_action)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")

    try:
        yield client, op_store, mock_log_action
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_list_routewise_settings_returns_curated_runtime_keys(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(
        side_effect=lambda key: {
            "routewise_latency_slo_sec": {"value": "1.5", "value_type": "float"},
        }.get(key)
    )

    response = await client.get(
        "/admin/routewise/settings",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    items = response.json()["settings"]
    keys = [item["key"] for item in items]
    assert keys == [
        "routewise_latency_slo_sec",
        "routewise_latency_min_samples",
    ]
    slo = next(item for item in items if item["key"] == "routewise_latency_slo_sec")
    assert slo["value"] == 1.5


@pytest.mark.asyncio
async def test_patch_routewise_setting_validates_and_persists(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)
    op_store.set_setting = AsyncMock()

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_min_samples",
        json={"value": 12},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.set_setting.assert_awaited_once_with(
        "routewise_latency_min_samples",
        "12",
        "int",
        "127.0.0.1",
    )
    assert response.json()["value"] == 12


@pytest.mark.asyncio
async def test_patch_routewise_setting_refreshes_live_routewise_router(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(
        side_effect=lambda key: {
            "routewise_latency_slo_sec": {"value": "1.5", "value_type": "float"},
            "routewise_latency_min_samples": {"value": "8", "value_type": "int"},
        }.get(key)
    )
    op_store.set_setting = AsyncMock()

    runtime_settings = client._transport.app.state.services.runtime_settings
    cached_at = time.monotonic()
    runtime_settings._cache.update(
        {
            "routewise_latency_slo_sec": (cached_at, 1.5),
            "routewise_latency_min_samples": (cached_at, 8),
        }
    )

    router = RouteWiseRouter(config=RouteWiseConfig())
    registry = ModelRouterRegistry(models_config={})
    registry._cache["test-model"] = router
    client._transport.app.state.services.model_router_registry = registry

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_slo_sec",
        json={"value": 1.5},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert router.config.latency_slo_sec == 1.5
    assert router.config.latency_min_samples == 8


@pytest.mark.asyncio
async def test_patch_routewise_setting_refreshes_uncached_routewise_router(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(
        side_effect=lambda key: {
            "routewise_latency_slo_sec": {"value": "1.5", "value_type": "float"},
            "routewise_latency_min_samples": {"value": "8", "value_type": "int"},
        }.get(key)
    )
    op_store.set_setting = AsyncMock()

    runtime_settings = client._transport.app.state.services.runtime_settings
    runtime_settings._cache.update(
        {
            "routewise_latency_slo_sec": (time.monotonic(), 9.9),
            "routewise_latency_min_samples": (time.monotonic(), 99),
        }
    )

    registry = ModelRouterRegistry(
        models_config={
            "uncached-model": {
                "router": "routewise",
                "router_params": {"budget_alpha": 0.5},
            }
        }
    )
    client._transport.app.state.services.model_router_registry = registry

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_min_samples",
        json={"value": 8},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    uncached_router = registry.get_router("uncached-model")
    assert isinstance(uncached_router, RouteWiseRouter)
    assert uncached_router.config.latency_slo_sec == 1.5
    assert uncached_router.config.latency_min_samples == 8


@pytest.mark.asyncio
async def test_routewise_patch_invalidates_all_routewise_cache_keys(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(
        side_effect=lambda key: {
            "routewise_latency_slo_sec": {"value": "1.5", "value_type": "float"},
            "routewise_latency_min_samples": {"value": "8", "value_type": "int"},
        }.get(key)
    )
    op_store.set_setting = AsyncMock()

    runtime_settings = client._transport.app.state.services.runtime_settings
    runtime_settings._cache.update(
        {
            "routewise_latency_slo_sec": (time.monotonic(), 9.9),
            "routewise_latency_min_samples": (time.monotonic(), 99),
        }
    )

    router = RouteWiseRouter(config=RouteWiseConfig())
    registry = ModelRouterRegistry(models_config={})
    registry._cache["test-model"] = router
    client._transport.app.state.services.model_router_registry = registry

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_min_samples",
        json={"value": 8},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert router.config.latency_slo_sec == 1.5
    assert router.config.latency_min_samples == 8


@pytest.mark.asyncio
async def test_patch_routewise_daily_quota_no_longer_exposed(admin_client):
    """Quota limits are route-level config now, not an admin runtime setting."""
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.patch(
        "/admin/routewise/settings/routewise_daily_quota",
        json={"value": 5000},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 404
    assert "Unknown setting" in response.json()["detail"]


@pytest.mark.asyncio
async def test_patch_routewise_setting_rejects_unknown_routewise_key(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.patch(
        "/admin/routewise/settings/routewise_unknown",
        json={"value": "pd"},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 404
    assert "Unknown setting" in response.json()["detail"]


@pytest.mark.asyncio
async def test_patch_routewise_setting_rejects_latency_slo_below_min(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_slo_sec",
        json={"value": 0.05},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 400
    assert "below min" in response.json()["detail"]


@pytest.mark.asyncio
async def test_patch_routewise_setting_rejects_boolean_for_int(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_min_samples",
        json={"value": True},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 400
    assert "expects an integer value" in response.json()["detail"]


@pytest.mark.asyncio
async def test_patch_routewise_setting_rejects_unknown_key(admin_client):
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/routewise/settings/not-a-real-key",
        json={"value": 1},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_generic_admin_settings_rejects_routewise_keys(admin_client):
    client, _, _ = admin_client

    response = await client.patch(
        "/admin/settings/routewise_latency_slo_sec",
        json={"value": 2.5},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 404
