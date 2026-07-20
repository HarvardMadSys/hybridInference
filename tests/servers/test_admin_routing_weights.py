"""Tests for admin route weight override endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from routing.model_router_registry import ModelRouterRegistry
from serving.config.weight_overrides import WeightOverrideResolver
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import routing_weights


def _adapter(model_id: str, provider: str, endpoint_id: str):
    adapter = MagicMock()
    adapter.config.id = model_id
    adapter.config.name = model_id
    adapter.config.provider = provider
    adapter.config.base_url = f"http://{provider}.test"
    adapter.config.endpoint_id = endpoint_id
    return adapter


def _row_by_endpoint(rows: list[dict], endpoint_id: str) -> dict:
    return next(row for row in rows if row["endpoint_id"] == endpoint_id)


def test_routewise_rebuild_falls_back_to_legacy_hook() -> None:
    legacy_rebuild = MagicMock()
    legacy_router = SimpleNamespace(
        _rebuild_from_fixed_router=legacy_rebuild,
        _route_commit_lock=None,
    )
    registry = SimpleNamespace(cached_routers=lambda: [legacy_router])
    services = SimpleNamespace(model_router_registry=registry)

    routing_weights._rebuild_routewise_routers(services)

    legacy_rebuild.assert_called_once_with()


@pytest.fixture
async def admin_client(monkeypatch):
    op_store = MagicMock()
    op_store.list_weight_overrides_for_model = AsyncMock(return_value=[])
    op_store.list_all_weight_overrides = AsyncMock(return_value=[])
    op_store.upsert_weight_override = AsyncMock()
    op_store.delete_weight_override = AsyncMock(return_value=True)
    op_store.get_user_by_id = AsyncMock(return_value=None)
    op_store.create_audit_log = AsyncMock()

    router = RouteExecutor()
    local = _adapter("public-model", "local", "public-model:local")
    remote = _adapter("public-model", "remote", "public-model:remote")
    router.register_route(
        "public-model",
        [(local, 1.0), (remote, 2.0)],
        aliases=["public-model-alias"],
    )
    slash = _adapter("provider/model", "remote", "provider/model:remote")
    router.register_route("provider/model", [(slash, 3.0)])
    disabled = _adapter("zero-model", "disabled", "zero-model:disabled")
    active = _adapter("zero-model", "active", "zero-model:active")
    router.register_route("zero-model", [(disabled, 0.0), (active, 1.0)])
    model_router_registry = ModelRouterRegistry(
        models_config={
            "public-model": {"router": "routewise"},
            "provider/model": {"router": "fixed"},
            "zero-model": {"router": "fixed"},
        },
        default_router_name="fixed",
    )
    model_router_registry.bind_fixed_router(router)

    app = FastAPI()
    resolver = WeightOverrideResolver(op_store)
    app.state.services = AppServices(
        router=router,
        model_router_registry=model_router_registry,
        operational_store=op_store,
        weight_override_resolver=resolver,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setattr(
        "serving.servers.routers.admin.routing_weights.log_admin_action",
        AsyncMock(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, op_store, resolver, model_router_registry


@pytest.mark.asyncio
async def test_auth_required(admin_client):
    client, _, _, _ = admin_client

    response = await client.get("/admin/routing/weights/public-model")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_route_weights_returns_yaml_override_and_effective_weights(admin_client):
    client, op_store, _, model_router_registry = admin_client
    op_store.list_weight_overrides_for_model.return_value = [
        {"model_id": "public-model", "endpoint_id": "public-model:remote", "weight": 4.0}
    ]
    model_router_registry.get_router = MagicMock(
        side_effect=AssertionError("should not call get_router")
    )

    response = await client.get(
        "/admin/routing/weights/public-model",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    rows = response.json()["routes"]
    local = _row_by_endpoint(rows, "public-model:local")
    remote = _row_by_endpoint(rows, "public-model:remote")
    assert local["yaml_weight"] == 1.0
    assert local["strategy"] == "routewise"
    assert local["override_weight"] is None
    assert local["effective_weight"] == 1.0
    assert remote["yaml_weight"] == 2.0
    assert remote["strategy"] == "routewise"
    assert remote["override_weight"] == 4.0
    assert remote["effective_weight"] == 4.0


@pytest.mark.asyncio
async def test_get_all_route_weights_returns_canonical_models_only(admin_client):
    client, op_store, _, _ = admin_client
    op_store.list_all_weight_overrides.return_value = [
        {"model_id": "public-model", "endpoint_id": "public-model:remote", "weight": 4.0}
    ]
    op_store.list_weight_overrides_for_model.reset_mock()

    response = await client.get(
        "/admin/routing/weights",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    rows = response.json()["routes"]
    model_ids = {row["model_id"] for row in rows}
    remote = _row_by_endpoint(rows, "public-model:remote")
    slash = _row_by_endpoint(rows, "provider/model:remote")
    assert model_ids == {"public-model", "provider/model", "zero-model"}
    assert remote["strategy"] == "routewise"
    assert slash["strategy"] == "fixed"
    assert remote["override_weight"] == 4.0
    op_store.list_all_weight_overrides.assert_awaited_once_with()
    op_store.list_weight_overrides_for_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_route_weight_upserts_and_invalidates_cache(admin_client):
    client, op_store, resolver, model_router_registry = admin_client
    await resolver.get_for_model("public-model")
    assert op_store.list_weight_overrides_for_model.await_count == 1
    routewise_router = MagicMock()
    routewise_router._route_commit_lock = None
    model_router_registry.cached_routers = MagicMock(return_value=[routewise_router])
    model_router_registry.get_router = MagicMock(
        side_effect=AssertionError("should not call get_router")
    )

    response = await client.put(
        "/admin/routing/weights/public-model/public-model:remote",
        json={"weight": 4.5},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["strategy"] == "routewise"
    op_store.upsert_weight_override.assert_awaited_once_with(
        "public-model", "public-model:remote", 4.5, "127.0.0.1"
    )
    op_store.list_weight_overrides_for_model.return_value = [
        {"model_id": "public-model", "endpoint_id": "public-model:remote", "weight": 4.5}
    ]
    assert await resolver.get_for_model("public-model") == {"public-model:remote": 4.5}
    routewise_router._rebuild_from_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_put_rejects_negative_unknown_endpoint_and_all_zero(admin_client):
    client, op_store, _, _ = admin_client
    headers = {"Authorization": "Bearer test-admin"}

    negative = await client.put(
        "/admin/routing/weights/public-model/public-model:remote",
        json={"weight": -0.1},
        headers=headers,
    )
    assert negative.status_code == 400

    unknown_endpoint = await client.put(
        "/admin/routing/weights/public-model/does-not-exist",
        json={"weight": 1},
        headers=headers,
    )
    assert unknown_endpoint.status_code == 400

    zero_local = await client.put(
        "/admin/routing/weights/public-model/public-model:local",
        json={"weight": 0},
        headers=headers,
    )
    assert zero_local.status_code == 200
    op_store.list_weight_overrides_for_model.return_value = [
        {"model_id": "public-model", "endpoint_id": "public-model:local", "weight": 0.0}
    ]

    all_zero = await client.put(
        "/admin/routing/weights/public-model/public-model:remote",
        json={"weight": 0},
        headers=headers,
    )
    assert all_zero.status_code == 400
    assert all_zero.json()["detail"] == "cannot zero all routes for model"


@pytest.mark.asyncio
async def test_unknown_model_and_alias_return_404(admin_client):
    client, _, _, _ = admin_client
    headers = {"Authorization": "Bearer test-admin"}

    unknown = await client.get("/admin/routing/weights/unknown", headers=headers)
    alias = await client.get("/admin/routing/weights/public-model-alias", headers=headers)

    assert unknown.status_code == 404
    assert alias.status_code == 404


@pytest.mark.asyncio
async def test_delete_route_weight_clears_override(admin_client):
    client, op_store, _, model_router_registry = admin_client
    op_store.list_weight_overrides_for_model.return_value = [
        {"model_id": "public-model", "endpoint_id": "public-model:remote", "weight": 4.0}
    ]
    routewise_router = MagicMock()
    routewise_router._route_commit_lock = None
    model_router_registry.cached_routers = MagicMock(return_value=[routewise_router])
    model_router_registry.get_router = MagicMock(
        side_effect=AssertionError("should not call get_router")
    )

    response = await client.delete(
        "/admin/routing/weights/public-model/public-model:remote",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.delete_weight_override.assert_awaited_once_with("public-model", "public-model:remote")
    row = response.json()
    assert row["endpoint_id"] == "public-model:remote"
    assert row["strategy"] == "routewise"
    assert row["override_weight"] is None
    assert row["effective_weight"] == 2.0
    routewise_router._rebuild_from_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_delete_route_weight_rejects_all_zero_effective_weights(admin_client):
    client, op_store, _, _ = admin_client
    op_store.list_weight_overrides_for_model.return_value = [
        {"model_id": "zero-model", "endpoint_id": "zero-model:disabled", "weight": 1.0},
        {"model_id": "zero-model", "endpoint_id": "zero-model:active", "weight": 0.0},
    ]

    response = await client.delete(
        "/admin/routing/weights/zero-model/zero-model:disabled",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "cannot zero all routes for model"
    op_store.delete_weight_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_route_weight_supports_slash_model_and_endpoint_ids(admin_client):
    client, op_store, _, _ = admin_client

    response = await client.put(
        "/admin/routing/weights/provider/model/provider/model:remote",
        json={"weight": 5},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    op_store.upsert_weight_override.assert_awaited_with(
        "provider/model", "provider/model:remote", 5.0, "127.0.0.1"
    )
