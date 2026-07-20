"""Tests for admin provider availability (disable/enable) endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from routing.model_router_registry import ModelRouterRegistry
from serving.adapters import ModelConfig
from serving.config.disabled_providers import DisabledProviderResolver
from serving.servers import bootstrap
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router


def _adapter(provider: str, endpoint_id: str) -> MagicMock:
    adapter = MagicMock()
    adapter.config = ModelConfig(
        id="model-a",
        name=f"{provider} Model",
        provider=provider,
        base_url=f"https://{provider}.example/v1",
        endpoint_id=endpoint_id,
    )
    return adapter


@pytest.fixture
async def admin_client(monkeypatch):
    disabled_rows: list[dict] = []

    op_store = MagicMock()
    op_store.list_disabled_providers = AsyncMock(side_effect=lambda: list(disabled_rows))

    async def _set(provider: str, updated_by):
        if not any(r["provider"] == provider for r in disabled_rows):
            disabled_rows.append({"provider": provider})

    async def _clear(provider: str) -> bool:
        before = len(disabled_rows)
        disabled_rows[:] = [r for r in disabled_rows if r["provider"] != provider]
        return len(disabled_rows) < before

    op_store.set_provider_disabled = AsyncMock(side_effect=_set)
    op_store.clear_provider_disabled = AsyncMock(side_effect=_clear)

    router = RouteExecutor()
    router.register_route(
        "model-a",
        [(_adapter("openrouter", "openrouter:h:1"), 0.6), (_adapter("zai", "zai:h:1"), 0.4)],
    )
    router.register_route("model-b", [(_adapter("openrouter", "openrouter:h:2"), 1.0)])

    resolver = DisabledProviderResolver(op_store)
    await resolver.load_all()
    router.disabled_provider_resolver = resolver
    model_router_registry = ModelRouterRegistry(
        models_config={
            "model-a": {"router": "routewise"},
            "model-b": {"router": "routewise"},
        },
        shared_fixed_router=router,
    )
    model_router_registry.get_router("model-a")
    model_router_registry.get_router("model-b")

    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        model_router_registry=model_router_registry,
        operational_store=op_store,
        disabled_provider_resolver=resolver,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setattr(
        "serving.servers.routers.admin.providers.log_admin_action",
        AsyncMock(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, op_store, resolver, model_router_registry


@pytest.mark.asyncio
async def test_list_routable_providers_aggregates_models_and_endpoints(admin_client):
    client, _, _, _ = admin_client

    response = await client.get(
        "/admin/providers/routable",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    by_name = {p["provider"]: p for p in response.json()["providers"]}
    assert by_name["openrouter"]["model_count"] == 2
    assert by_name["openrouter"]["endpoint_count"] == 2
    assert by_name["openrouter"]["disabled"] is False
    assert by_name["zai"]["model_count"] == 1


@pytest.mark.asyncio
async def test_disable_provider_persists_and_updates_resolver(admin_client):
    client, op_store, resolver, _ = admin_client

    response = await client.patch(
        "/admin/providers/openrouter/disabled",
        json={"disabled": True},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openrouter"
    assert body["disabled"] is True
    assert body["affected_model_count"] == 2
    op_store.set_provider_disabled.assert_awaited_once_with("openrouter", "127.0.0.1")
    assert resolver.is_disabled("openrouter")


@pytest.mark.asyncio
async def test_enable_provider_clears_state(admin_client):
    client, op_store, resolver, _ = admin_client
    resolver.set_disabled("openrouter")

    response = await client.patch(
        "/admin/providers/openrouter/disabled",
        json={"disabled": False},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 200
    assert response.json()["disabled"] is False
    op_store.clear_provider_disabled.assert_awaited_once_with("openrouter")
    assert not resolver.is_disabled("openrouter")


@pytest.mark.asyncio
async def test_disable_unknown_provider_returns_404(admin_client):
    client, _, _, _ = admin_client

    response = await client.patch(
        "/admin/providers/nonexistent/disabled",
        json={"disabled": True},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_disable_and_reenable_provider_rebuilds_routewise_candidates(admin_client):
    client, _, _, registry = admin_client
    routewise = registry.get_router("model-a")

    assert {
        candidate.adapter.config.provider for candidate in routewise.route_candidates["model-a"]
    } == {"openrouter", "zai"}

    disabled = await client.patch(
        "/admin/providers/openrouter/disabled",
        json={"disabled": True},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert disabled.status_code == 200
    assert [
        candidate.adapter.config.provider for candidate in routewise.route_candidates["model-a"]
    ] == ["zai"]
    assert routewise.route_candidates["model-b"] == []

    enabled = await client.patch(
        "/admin/providers/openrouter/disabled",
        json={"disabled": False},
        headers={"Authorization": "Bearer test-admin"},
    )

    assert enabled.status_code == 200
    assert {
        candidate.adapter.config.provider for candidate in routewise.route_candidates["model-a"]
    } == {"openrouter", "zai"}
    assert [
        candidate.adapter.config.provider for candidate in routewise.route_candidates["model-b"]
    ] == ["openrouter"]


@pytest.mark.asyncio
async def test_background_snapshot_refresh_rebuilds_routewise_candidates(admin_client):
    _, op_store, resolver, registry = admin_client
    routewise = registry.get_router("model-a")
    refresh_state = bootstrap._EffectiveRouteRefreshState()

    op_store.list_disabled_providers.side_effect = lambda: [{"provider": "openrouter"}]
    assert (
        await bootstrap._reload_effective_route_state(
            resolver,
            registry,
            refresh_state,
        )
        is True
    )
    assert [
        candidate.adapter.config.provider for candidate in routewise.route_candidates["model-a"]
    ] == ["zai"]

    op_store.list_disabled_providers.side_effect = lambda: []
    assert (
        await bootstrap._reload_effective_route_state(
            resolver,
            registry,
            refresh_state,
        )
        is True
    )
    assert {
        candidate.adapter.config.provider for candidate in routewise.route_candidates["model-a"]
    } == {"openrouter", "zai"}
