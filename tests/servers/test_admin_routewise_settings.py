"""Tests for dedicated admin Routewise settings endpoints."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.model_router_registry import ModelRouterRegistry
from routing.routers import FixedRouter
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseProbeResult, RouteWiseRouter
from serving.config.routewise_model_settings import (
    RouteWiseSettingsResolver,
    model_routewise_setting_key,
)
from serving.config.runtime_settings import RuntimeSettings
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import routewise as routewise_admin


async def _install_model_settings_registry(
    client,
    op_store,
    *,
    models_config,
    alias_to_model=None,
):
    rows: dict[str, dict[str, str]] = {}

    async def get_setting(key):
        return rows.get(key)

    async def set_setting(key, value, value_type, modified_by):
        rows[key] = {
            "value": value,
            "value_type": value_type,
            "modified_by": modified_by,
        }

    async def delete_setting(key):
        return rows.pop(key, None) is not None

    async def list_settings():
        return [{"key": key, **row} for key, row in rows.items()]

    op_store.get_setting = AsyncMock(side_effect=get_setting)
    op_store.set_setting = AsyncMock(side_effect=set_setting)
    op_store.delete_setting = AsyncMock(side_effect=delete_setting)
    op_store.list_settings = AsyncMock(side_effect=list_settings)

    registry = ModelRouterRegistry(
        models_config=models_config,
        alias_to_model=alias_to_model,
        shared_fixed_router=FixedRouter(),
    )
    services = client._transport.app.state.services
    services.model_router_registry = registry
    resolver = RouteWiseSettingsResolver(op_store, services.runtime_settings, registry)
    await resolver.load_all()
    services.routewise_settings_resolver = resolver
    return registry, resolver, rows


@pytest.fixture
def mock_stores():
    op_store = MagicMock()
    op_store.get_setting = AsyncMock(return_value=None)
    op_store.set_setting = AsyncMock()
    op_store.delete_setting = AsyncMock(return_value=False)
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
        model_router_registry=ModelRouterRegistry(
            models_config={},
            shared_fixed_router=FixedRouter(),
        ),
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
            "routewise_budget_alpha": {"value": "0.6", "value_type": "float"},
            "routewise_latency_slo_sec": {"value": "1.5", "value_type": "float"},
            "routewise_latency_min_samples": {"value": "10", "value_type": "int"},
            "routewise_probe_enabled": {"value": "false", "value_type": "bool"},
            "routewise_probe_interval_sec": {"value": "300.0", "value_type": "float"},
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
        "routewise_budget_alpha",
        "routewise_latency_slo_sec",
        "routewise_latency_min_samples",
        "routewise_probe_enabled",
        "routewise_probe_interval_sec",
    ]
    alpha = next(item for item in items if item["key"] == "routewise_budget_alpha")
    assert alpha["value"] == 0.6
    slo = next(item for item in items if item["key"] == "routewise_latency_slo_sec")
    assert slo["value"] == 1.5
    probe_enabled = next(item for item in items if item["key"] == "routewise_probe_enabled")
    assert probe_enabled["value"] is False


@pytest.mark.asyncio
async def test_model_settings_get_canonicalizes_alias_without_building_router(admin_client):
    client, op_store, _ = admin_client
    registry, _resolver, _rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={
            "model-a": {
                "router": "routewise",
                "router_params": {"budget_alpha": 0.2},
            }
        },
        alias_to_model={"model/a": "model-a"},
    )

    response = await client.get(
        "/admin/routewise/model-settings",
        params={"model_id": "model/a"},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["model_id"] == "model-a"
    alpha = response.json()["settings"][0]
    assert alpha == {
        "key": "routewise_budget_alpha",
        "value": 0.2,
        "value_type": "float",
        "default_value": 0.2,
        "source": "model_config",
        "overridden": False,
        "description": (
            "RouteWise LP cost budget interpolation: 0 favors cheapest feasible routes, "
            "1 allows the full effective-cost range."
        ),
        "min": 0.0,
        "max": 1.0,
    }
    assert registry.registered_models() == {}


@pytest.mark.asyncio
async def test_model_settings_patch_isolates_models_and_persists_canonical_key(admin_client):
    client, op_store, audit = admin_client
    registry, _resolver, rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={
            "model-a": {
                "router": "routewise",
                "router_params": {"budget_alpha": 0.2},
            },
            "model-b": {
                "router": "routewise",
                "router_params": {"budget_alpha": 0.8},
            },
        },
        alias_to_model={"model/a": "model-a"},
    )
    router_a = registry.get_router("model-a")
    router_b = registry.get_router("model-b")

    response = await client.patch(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "model/a"},
        json={"value": 0.4},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["value"] == 0.4
    assert response.json()["default_value"] == 0.2
    assert response.json()["source"] == "runtime_override"
    assert response.json()["overridden"] is True
    assert isinstance(router_a, RouteWiseRouter)
    assert isinstance(router_b, RouteWiseRouter)
    assert router_a.config.budget_alpha == 0.4
    assert router_b.config.budget_alpha == 0.8
    scoped_key = model_routewise_setting_key("routewise_budget_alpha", "model-a")
    assert rows[scoped_key]["value"] == "0.4"
    assert model_routewise_setting_key("routewise_budget_alpha", "model/a") not in rows
    assert audit.await_args.args[4]["admin_id"]


@pytest.mark.asyncio
async def test_model_settings_reset_is_idempotent_and_restores_yaml(admin_client):
    client, op_store, _ = admin_client
    _registry, _resolver, rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={
            "model-a": {
                "router": "routewise",
                "router_params": {"budget_alpha": 0.25},
            }
        },
    )
    headers = {"Authorization": "Bearer test-admin"}
    await client.patch(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "model-a"},
        json={"value": 0.4},
        headers=headers,
    )

    first = await client.delete(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "model-a"},
        headers=headers,
    )
    second = await client.delete(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "model-a"},
        headers=headers,
    )

    for response in (first, second):
        assert response.status_code == 200
        assert response.json()["value"] == 0.25
        assert response.json()["default_value"] == 0.25
        assert response.json()["source"] == "model_config"
        assert response.json()["overridden"] is False
    assert model_routewise_setting_key("routewise_budget_alpha", "model-a") not in rows


@pytest.mark.asyncio
async def test_model_settings_patch_keeps_durable_value_when_live_apply_fails(admin_client):
    client, op_store, _ = admin_client
    registry, resolver, rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={
            "model-a": {
                "router": "routewise",
                "router_params": {"budget_alpha": 0.2},
            }
        },
    )
    router = registry.get_router("model-a")
    assert isinstance(router, RouteWiseRouter)
    router.refresh_probe_task = AsyncMock(side_effect=RuntimeError("probe refresh failed"))

    response = await client.patch(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "model-a"},
        json={"value": 0.4},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["value"] == 0.4
    scoped_key = model_routewise_setting_key("routewise_budget_alpha", "model-a")
    assert rows[scoped_key]["value"] == "0.4"
    resolved = await resolver.get_resolved("model-a", "routewise_budget_alpha")
    assert resolved.value == 0.4
    assert resolved.source == "runtime_override"
    assert router.config.budget_alpha == 0.4


@pytest.mark.asyncio
async def test_model_settings_reset_keeps_durable_delete_when_live_apply_fails(admin_client):
    client, op_store, _ = admin_client
    registry, resolver, rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={
            "model-a": {
                "router": "routewise",
                "router_params": {"budget_alpha": 0.2},
            }
        },
    )
    router = registry.get_router("model-a")
    assert isinstance(router, RouteWiseRouter)
    headers = {"Authorization": "Bearer test-admin"}
    response = await client.patch(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "model-a"},
        json={"value": 0.4},
        headers=headers,
    )
    assert response.status_code == 200
    router.refresh_probe_task = AsyncMock(side_effect=RuntimeError("probe refresh failed"))

    reset = await client.delete(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "model-a"},
        headers=headers,
    )

    assert reset.status_code == 200
    assert reset.json()["value"] == 0.2
    scoped_key = model_routewise_setting_key("routewise_budget_alpha", "model-a")
    assert scoped_key not in rows
    resolved = await resolver.get_resolved("model-a", "routewise_budget_alpha")
    assert resolved.value == 0.2
    assert resolved.source == "model_config"
    assert router.config.budget_alpha == 0.2


@pytest.mark.asyncio
async def test_model_settings_audit_failure_does_not_reject_committed_update(admin_client):
    client, op_store, audit = admin_client
    _registry, _resolver, rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={"model-a": {"router": "routewise"}},
    )
    audit.side_effect = RuntimeError("audit unavailable")

    response = await client.patch(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "model-a"},
        json={"value": 0.4},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["value"] == 0.4
    assert rows[model_routewise_setting_key("routewise_budget_alpha", "model-a")]["value"] == "0.4"


@pytest.mark.asyncio
async def test_model_settings_accept_known_fixed_model_without_building_router(admin_client):
    client, op_store, _ = admin_client
    registry, _resolver, rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={"fixed-model": {"router": "fixed"}},
    )

    response = await client.patch(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "fixed-model"},
        json={"value": 0.35},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert registry.registered_models() == {}
    assert (
        rows[model_routewise_setting_key("routewise_budget_alpha", "fixed-model")]["value"]
        == "0.35"
    )


@pytest.mark.asyncio
async def test_unknown_model_settings_request_does_not_build_ghost_router(admin_client):
    client, op_store, _ = admin_client
    registry, _resolver, rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={"known-model": {"router": "fixed"}},
    )

    response = await client.patch(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "ghost/model"},
        json={"value": 0.35},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 404
    assert registry.registered_models() == {}
    assert rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_model_setting_write_returns_404_if_runtime_model_is_deleted_while_waiting(
    admin_client,
    monkeypatch,
    method,
):
    client, op_store, _ = admin_client
    registry, _resolver, _rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={"runtime-model": {"router": "fixed"}},
    )
    services = client._transport.app.state.services
    transition_lock = services.model_router_transition_locks.setdefault(
        "runtime-model",
        asyncio.Lock(),
    )
    await transition_lock.acquire()

    first_validation = asyncio.Event()
    validation_calls = 0
    original_validate = routewise_admin._canonical_model_id_or_404

    def track_validation(resolver, model_id):
        nonlocal validation_calls
        try:
            result = original_validate(resolver, model_id)
        finally:
            validation_calls += 1
        if validation_calls == 1:
            first_validation.set()
        return result

    monkeypatch.setattr(routewise_admin, "_canonical_model_id_or_404", track_validation)
    kwargs = {
        "params": {"model_id": "runtime-model"},
        "headers": {"Authorization": "Bearer test-admin"},
    }
    if method == "PATCH":
        kwargs["json"] = {"value": 0.4}
    request = asyncio.create_task(
        client.request(
            method,
            "/admin/routewise/model-settings/routewise_budget_alpha",
            **kwargs,
        )
    )
    await first_validation.wait()

    # Model teardown owns the same lock and removes registry existence before
    # releasing it to the queued settings request.
    registry._configs.pop("runtime-model")
    transition_lock.release()
    response = await request

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown model: runtime-model"
    assert validation_calls == 2
    op_store.set_setting.assert_not_awaited()
    op_store.delete_setting.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_patch_respects_scoped_override_and_yaml_params(admin_client):
    client, op_store, _ = admin_client
    registry, _resolver, _rows = await _install_model_settings_registry(
        client,
        op_store,
        models_config={
            "model-a": {"router": "routewise"},
            "model-b": {
                "router": "routewise",
                "router_params": {"budget_alpha": 0.8},
            },
            "model-c": {"router": "routewise"},
        },
    )
    router_a = registry.get_router("model-a")
    router_b = registry.get_router("model-b")
    router_c = registry.get_router("model-c")
    headers = {"Authorization": "Bearer test-admin"}
    scoped = await client.patch(
        "/admin/routewise/model-settings/routewise_budget_alpha",
        params={"model_id": "model-a"},
        json={"value": 0.4},
        headers=headers,
    )
    assert scoped.status_code == 200

    response = await client.patch(
        "/admin/routewise/settings/routewise_budget_alpha",
        json={"value": 0.6},
        headers=headers,
    )

    assert response.status_code == 200
    assert isinstance(router_a, RouteWiseRouter)
    assert isinstance(router_b, RouteWiseRouter)
    assert isinstance(router_c, RouteWiseRouter)
    assert router_a.config.budget_alpha == 0.4
    assert router_b.config.budget_alpha == 0.8
    assert router_c.config.budget_alpha == 0.6


@pytest.mark.asyncio
async def test_patch_routewise_setting_validates_and_persists(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)
    op_store.set_setting = AsyncMock()

    response = await client.patch(
        "/admin/routewise/settings/routewise_budget_alpha",
        json={"value": 0.4},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.set_setting.assert_awaited_once_with(
        "routewise_budget_alpha",
        "0.4",
        "float",
        "127.0.0.1",
    )
    assert response.json()["value"] == 0.4


@pytest.mark.asyncio
async def test_global_patch_is_successful_when_immediate_live_apply_fails(
    admin_client,
    monkeypatch,
):
    client, op_store, _ = admin_client
    refresh = AsyncMock(side_effect=RuntimeError("transient apply failure"))
    monkeypatch.setattr(routewise_admin, "_refresh_live_routewise_routers", refresh)

    response = await client.patch(
        "/admin/routewise/settings/routewise_budget_alpha",
        json={"value": 0.4},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.set_setting.assert_awaited_once_with(
        "routewise_budget_alpha",
        "0.4",
        "float",
        "127.0.0.1",
    )


@pytest.mark.asyncio
async def test_patch_routewise_setting_refreshes_live_routewise_router(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(
        side_effect=lambda key: {
            "routewise_budget_alpha": {"value": "0.25", "value_type": "float"},
            "routewise_latency_slo_sec": {"value": "1.5", "value_type": "float"},
        }.get(key)
    )
    op_store.set_setting = AsyncMock()
    op_store.list_settings = AsyncMock(
        return_value=[
            {
                "key": "routewise_budget_alpha",
                "value": "0.25",
                "value_type": "float",
            },
            {
                "key": "routewise_latency_slo_sec",
                "value": "1.5",
                "value_type": "float",
            },
        ]
    )

    runtime_settings = client._transport.app.state.services.runtime_settings
    cached_at = time.monotonic()
    runtime_settings._cache.update(
        {
            "routewise_budget_alpha": (cached_at, 0.75),
            "routewise_latency_slo_sec": (cached_at, 1.5),
        }
    )

    router = RouteWiseRouter(config=RouteWiseConfig())
    registry = ModelRouterRegistry(models_config={"test-model": {"router": "routewise"}})
    registry._cache["test-model"] = router
    client._transport.app.state.services.model_router_registry = registry

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_slo_sec",
        json={"value": 1.5},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert router.config.budget_alpha == 0.25
    assert router.config.latency_slo_sec == 1.5
    assert router.config.latency_min_samples == 10


@pytest.mark.asyncio
async def test_global_patch_does_not_build_or_override_uncached_yaml_model(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(
        side_effect=lambda key: {
            "routewise_budget_alpha": {"value": "0.25", "value_type": "float"},
            "routewise_latency_slo_sec": {"value": "1.5", "value_type": "float"},
        }.get(key)
    )
    op_store.set_setting = AsyncMock()
    op_store.list_settings = AsyncMock(
        return_value=[
            {
                "key": "routewise_budget_alpha",
                "value": "0.25",
                "value_type": "float",
            },
            {
                "key": "routewise_latency_slo_sec",
                "value": "1.5",
                "value_type": "float",
            },
        ]
    )

    runtime_settings = client._transport.app.state.services.runtime_settings
    runtime_settings._cache.update(
        {
            "routewise_budget_alpha": (time.monotonic(), 0.9),
            "routewise_latency_slo_sec": (time.monotonic(), 9.9),
        }
    )

    registry = ModelRouterRegistry(
        models_config={
            "uncached-model": {
                "router": "routewise",
                "router_params": {"budget_alpha": 0.5},
            }
        },
        shared_fixed_router=FixedRouter(),
    )
    client._transport.app.state.services.model_router_registry = registry

    response = await client.patch(
        "/admin/routewise/settings/routewise_budget_alpha",
        json={"value": 0.25},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert registry.registered_models() == {}
    uncached_router = registry.get_router("uncached-model")
    assert isinstance(uncached_router, RouteWiseRouter)
    assert uncached_router.config.budget_alpha == 0.5
    assert uncached_router.config.latency_slo_sec == 3.0
    assert uncached_router.config.latency_min_samples == 10


@pytest.mark.asyncio
async def test_routewise_patch_refreshes_canonical_and_alias_cache_once(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(
        side_effect=lambda key: {
            "routewise_budget_alpha": {"value": "0.25", "value_type": "float"},
            "routewise_latency_slo_sec": {"value": "1.5", "value_type": "float"},
        }.get(key)
    )
    op_store.set_setting = AsyncMock()
    op_store.list_settings = AsyncMock(
        return_value=[
            {
                "key": "routewise_budget_alpha",
                "value": "0.25",
                "value_type": "float",
            },
            {
                "key": "routewise_latency_slo_sec",
                "value": "1.5",
                "value_type": "float",
            },
        ]
    )

    runtime_settings = client._transport.app.state.services.runtime_settings
    runtime_settings._cache.update(
        {
            "routewise_budget_alpha": (time.monotonic(), 0.9),
            "routewise_latency_slo_sec": (time.monotonic(), 9.9),
        }
    )

    router = RouteWiseRouter(config=RouteWiseConfig())
    router.apply_runtime_overrides = MagicMock(wraps=router.apply_runtime_overrides)
    router.refresh_probe_task = AsyncMock()
    registry = ModelRouterRegistry(
        models_config={"test-model": {"router": "routewise"}},
        alias_to_model={"test-alias": "test-model"},
    )
    registry._cache["test-model"] = router
    registry._cache["test-alias"] = router
    client._transport.app.state.services.model_router_registry = registry

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_slo_sec",
        json={"value": 1.5},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert router.config.budget_alpha == 0.25
    assert router.config.latency_slo_sec == 1.5
    assert router.config.latency_min_samples == 10
    router.apply_runtime_overrides.assert_called_once()
    router.refresh_probe_task.assert_awaited_once_with()


def test_routewise_router_collection_rejects_invalid_runtime_override():
    """Runtime-created models must satisfy the same concrete type invariant."""
    same_named_router = type("RouteWiseRouter", (), {})()
    registry = ModelRouterRegistry(models_config={})
    registry._router_overrides["runtime-model"] = "routewise"
    registry._cache["runtime-model"] = same_named_router
    services = AppServices(router=MagicMock(), model_router_registry=registry)

    with pytest.raises(
        TypeError,
        match="routewise strategy returned RouteWiseRouter for model 'runtime-model'",
    ):
        routewise_admin._routewise_routers(services, None)


@pytest.mark.asyncio
async def test_patch_routewise_latency_min_samples_is_still_runtime_tunable(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_min_samples",
        json={"value": 12},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["value"] == 12


@pytest.mark.asyncio
async def test_list_routewise_probe_samples(admin_client):
    client, op_store, _ = admin_client
    op_store.list_routewise_probe_samples = AsyncMock(
        return_value=[
            {
                "model_id": "test-model",
                "endpoint_id": "test-model:api",
                "ttft_ms": 123.4,
                "ok": True,
                "error": None,
                "checked_at": "2026-06-22T00:00:00Z",
            }
        ]
    )

    response = await client.get(
        "/admin/routewise/probes?model_id=test-model",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["samples"][0]["endpoint_id"] == "test-model:api"
    op_store.list_routewise_probe_samples.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_routewise_probe_calls_live_router(admin_client):
    client, op_store, _ = admin_client
    route_table = MagicMock()
    route_table.iter_effective_routes.return_value = ()
    route_table.canonical_id.side_effect = lambda model_id: {"test-alias": "test-model"}.get(
        model_id, model_id
    )
    router = RouteWiseRouter(route_table=route_table, config=RouteWiseConfig())
    router.attach_operational_store = MagicMock(wraps=router.attach_operational_store)
    router.canonical_model_id = MagicMock(wraps=router.canonical_model_id)
    router.run_probe_once = AsyncMock(
        return_value=[
            RouteWiseProbeResult(
                model_id="test-model",
                endpoint_id="test-model:api",
                ok=True,
                ttft_ms=42.0,
            )
        ]
    )
    registry = ModelRouterRegistry(
        models_config={"test-model": {"router": "routewise"}},
        alias_to_model={"test-alias": "test-model"},
    )
    registry._cache["test-model"] = router
    client._transport.app.state.services.model_router_registry = registry

    response = await client.post(
        "/admin/routewise/probes/run",
        json={"model_id": "test-alias", "endpoint_id": "test-model:api", "idle_only": False},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["ttft_ms"] == 42.0
    router.attach_operational_store.assert_called_once_with(op_store)
    router.canonical_model_id.assert_called_once_with("test-alias")
    router.run_probe_once.assert_awaited_once_with(
        model_id="test-model",
        endpoint_id="test-model:api",
        idle_only=False,
    )


@pytest.mark.asyncio
async def test_run_routewise_probe_does_not_silently_skip_required_public_method(admin_client):
    client, _op_store, _ = admin_client
    router = RouteWiseRouter(config=RouteWiseConfig())
    router.attach_operational_store = None
    router.run_probe_once = AsyncMock(return_value=[])
    registry = ModelRouterRegistry(models_config={"test-model": {"router": "routewise"}})
    registry._cache["test-model"] = router
    client._transport.app.state.services.model_router_registry = registry

    with pytest.raises(TypeError, match="not callable"):
        await client.post(
            "/admin/routewise/probes/run",
            json={"model_id": "test-model", "idle_only": False},
            headers={"Authorization": "Bearer test-admin"},
        )

    router.run_probe_once.assert_not_awaited()


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
async def test_patch_routewise_setting_rejects_budget_alpha_above_max(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.patch(
        "/admin/routewise/settings/routewise_budget_alpha",
        json={"value": 1.5},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 400
    assert "above max" in response.json()["detail"]


@pytest.mark.asyncio
async def test_patch_routewise_setting_rejects_boolean_for_float(admin_client):
    client, op_store, _ = admin_client
    op_store.get_setting = AsyncMock(return_value=None)

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_slo_sec",
        json={"value": True},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 400
    assert "expects a numeric value" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity"])
async def test_patch_routewise_setting_rejects_non_finite_float_before_persisting(
    admin_client,
    non_finite,
):
    client, op_store, _ = admin_client

    response = await client.patch(
        "/admin/routewise/settings/routewise_latency_slo_sec",
        content=f'{{"value": {non_finite}}}',
        headers={
            "Authorization": "Bearer test-admin",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert "finite numeric value" in response.json()["detail"]
    op_store.set_setting.assert_not_awaited()


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
@pytest.mark.parametrize("key", routewise_admin.ROUTEWISE_KEYS)
async def test_generic_admin_settings_rejects_routewise_keys(admin_client, key):
    client, _, _ = admin_client

    response = await client.patch(
        f"/admin/settings/{key}",
        json={"value": 0.5},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_generic_admin_settings_list_excludes_all_routewise_keys(admin_client):
    client, _, _ = admin_client

    response = await client.get(
        "/admin/settings",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    keys = {item["key"] for item in response.json()["settings"]}
    assert keys.isdisjoint(routewise_admin.ROUTEWISE_KEYS)
