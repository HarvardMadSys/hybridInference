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
            "routewise_daily_quota": {"value": "7000", "value_type": "int"},
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
        "routewise_decision_rule",
        "routewise_daily_quota",
        "routewise_latency_slo_sec",
        "routewise_latency_min_samples",
    ]
    assert next(item for item in items if item["key"] == "routewise_daily_quota")["value"] == 7000


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
async def test_patch_routewise_setting_accepts_lapd_rule(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)
    op_store.set_setting = AsyncMock()

    response = await client.patch(
        "/admin/routewise/settings/routewise_decision_rule",
        json={"value": "lapd"},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.set_setting.assert_awaited_once_with(
        "routewise_decision_rule",
        "lapd",
        "str",
        "127.0.0.1",
    )
    assert response.json()["value"] == "lapd"


@pytest.mark.asyncio
async def test_patch_routewise_setting_refreshes_live_routewise_router(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(
        side_effect=lambda key: {
            "routewise_decision_rule": {"value": "lapd", "value_type": "str"},
        }.get(key)
    )
    op_store.set_setting = AsyncMock()

    runtime_settings = client._transport.app.state.services.runtime_settings
    cached_at = time.monotonic()
    runtime_settings._cache.update(
        {
            "routewise_decision_rule": (cached_at, "lapd"),
            "routewise_daily_quota": (cached_at, 4321),
            "routewise_latency_slo_sec": (cached_at, 1.5),
            "routewise_latency_min_samples": (cached_at, 8),
        }
    )

    router = RouteWiseRouter(config=RouteWiseConfig())
    router.quota_mgr.consume()
    router.quota_mgr.consume()
    original_quota_mgr = router.quota_mgr
    registry = ModelRouterRegistry(models_config={})
    registry._cache["test-model"] = router
    client._transport.app.state.services.model_router_registry = registry

    response = await client.patch(
        "/admin/routewise/settings/routewise_decision_rule",
        json={"value": "lapd"},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert router.config.decision_rule == "lapd"
    assert router.config.daily_quota == 4321
    assert router.config.latency_slo_sec == 1.5
    assert router.config.latency_min_samples == 8
    assert router.quota_mgr is not original_quota_mgr
    assert router.quota_mgr.remaining == 4319


@pytest.mark.asyncio
async def test_patch_routewise_daily_quota_preserves_consumed_usage(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(
        side_effect=lambda key: {
            "routewise_decision_rule": {"value": "pd", "value_type": "str"},
            "routewise_daily_quota": {"value": "2000", "value_type": "int"},
            "routewise_latency_slo_sec": {"value": "2.0", "value_type": "float"},
            "routewise_latency_min_samples": {"value": "10", "value_type": "int"},
        }.get(key)
    )
    op_store.set_setting = AsyncMock()

    runtime_settings = client._transport.app.state.services.runtime_settings
    cached_at = time.monotonic()
    runtime_settings._cache.update(
        {
            "routewise_decision_rule": (cached_at, "pd"),
            "routewise_daily_quota": (cached_at, 2000),
            "routewise_latency_slo_sec": (cached_at, 2.0),
            "routewise_latency_min_samples": (cached_at, 10),
        }
    )

    router = RouteWiseRouter(config=RouteWiseConfig(daily_quota=5000))
    router.quota_mgr.consume()
    router.quota_mgr.consume()
    router.quota_mgr.consume()
    registry = ModelRouterRegistry(models_config={})
    registry._cache["test-model"] = router
    client._transport.app.state.services.model_router_registry = registry

    response = await client.patch(
        "/admin/routewise/settings/routewise_daily_quota",
        json={"value": 2000},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert router.config.daily_quota == 2000
    assert router.quota_mgr.remaining == 1997


@pytest.mark.asyncio
async def test_patch_routewise_setting_rejects_unknown_decision_rule(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.patch(
        "/admin/routewise/settings/routewise_decision_rule",
        json={"value": "foo"},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 400
    assert "routewise_decision_rule" in response.json()["detail"]


@pytest.mark.asyncio
async def test_patch_routewise_setting_rejects_zero_daily_quota(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.patch(
        "/admin/routewise/settings/routewise_daily_quota",
        json={"value": 0},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 400
    assert "below min" in response.json()["detail"]


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
