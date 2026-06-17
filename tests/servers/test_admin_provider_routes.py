"""Tests for admin provider route override endpoints."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters import ModelConfig, OpenAICompatAdapter, dynamic_keys
from serving.servers.deps import AppServices
from serving.servers.registry import _make_adapter
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import provider_routes
from serving.servers.routers.admin.provider_routes import apply_persisted_provider_route_configs
from serving.storage.base import ProviderKeyRow

AUTH = {"Authorization": "Bearer test-admin"}
NOW = datetime(2026, 6, 16, tzinfo=timezone.utc)


def _compat_adapter(
    *,
    model_id: str = "minimax-fast",
    provider: str,
    endpoint_id: str,
    base_url: str,
    provider_model_id: str,
    provider_type: str = "on_demand",
    quota_pool: str | None = None,
    quota_source: dict | None = None,
    quota: dict | None = None,
    concurrency_pool: str | None = None,
    concurrency: dict | None = None,
):
    return OpenAICompatAdapter(
        ModelConfig(
            id=model_id,
            name=model_id,
            provider=provider,
            base_url=base_url,
            api_keys=[f"{provider}-env-key-1234567890"],
            provider_model_id=provider_model_id,
            endpoint_id=endpoint_id,
            provider_type=provider_type,
            route_metadata={"provider_type": provider_type},
            quota_pool=quota_pool,
            quota_source=quota_source,
            quota=quota,
            concurrency_pool=concurrency_pool,
            concurrency=concurrency,
        )
    )


def _openrouter_deepinfra_adapter():
    return _make_adapter(
        "openrouter[deepinfra]",
        {
            "id": "minimax-fast",
            "name": "minimax-fast",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": None,
            "api_keys": ["openrouter-env-key-1234567890"],
            "provider_model_id": "minimax/minimax-m2.5",
            "endpoint_id": "minimax-fast:openrouter[deepinfra]-api",
            "provider_type": "on_demand",
        },
    )


@pytest.fixture
async def admin_client(monkeypatch):
    dynamic_keys.reset()

    op_store = MagicMock()
    op_store.list_provider_route_configs_for_model = AsyncMock(return_value=[])
    op_store.list_all_provider_route_configs = AsyncMock(return_value=[])
    op_store.upsert_provider_route_config = AsyncMock()
    op_store.delete_provider_route_config = AsyncMock(return_value=True)
    op_store.get_provider_key_full = AsyncMock(return_value=None)
    op_store.list_provider_keys = AsyncMock(return_value=[])
    op_store.list_provider_keys_full = AsyncMock(return_value=[])
    op_store.get_user_by_id = AsyncMock(return_value=None)
    op_store.create_audit_log = AsyncMock()

    route_executor = RouteExecutor()
    chutes = _compat_adapter(
        provider="chutes",
        endpoint_id="minimax-fast:chutes-api",
        base_url="https://llm.chutes.ai/v1",
        provider_model_id="MiniMaxAI/MiniMax-M2.5-TEE",
        provider_type="quota",
        quota_pool="chutes-minimax-fast-daily",
        quota_source={
            "provider": "chutes",
            "usage_label": "Daily requests",
            "unit": "requests",
        },
        quota={"limit": 5000},
    )
    featherless = _compat_adapter(
        provider="featherless",
        endpoint_id="minimax-fast:featherless-api",
        base_url="https://api.featherless.ai/v1",
        provider_model_id="MiniMaxAI/MiniMax-M2.5",
        provider_type="concurrency",
        concurrency_pool="featherless-minimax-fast",
        concurrency={"limit": 1},
    )
    deepinfra = _openrouter_deepinfra_adapter()
    route_executor.register_route(
        "minimax-fast",
        [(chutes, 1.0), (featherless, 1.0), (deepinfra, 1.0)],
        aliases=["MiniMax-Fast"],
    )

    for provider, adapter in (
        ("chutes", chutes),
        ("featherless", featherless),
        ("openrouter", deepinfra),
    ):
        dynamic_keys.register_known_provider(provider)
        dynamic_keys.register_adapter_for_provider(provider, adapter)

    fake_routewise = MagicMock()
    fake_routewise._rebuild_from_fixed_router = MagicMock()
    registry = MagicMock()
    registry.get_router_name.return_value = "routewise"
    registry.cached_routers.return_value = [fake_routewise]

    app = FastAPI()
    app.state.services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)

    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setattr(
        "serving.servers.routers.admin.provider_routes.log_admin_action",
        AsyncMock(),
    )
    verify_mock = AsyncMock()
    monkeypatch.setattr(
        "serving.servers.routers.admin.provider_routes._verify_provider_route",
        verify_mock,
    )
    monkeypatch.setattr(
        provider_routes.socket,
        "getaddrinfo",
        lambda _host, port, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", port or 443),
            )
        ],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, op_store, route_executor, fake_routewise, verify_mock

    dynamic_keys.reset()


@pytest.mark.asyncio
async def test_get_provider_routes_lists_routewise_candidates(admin_client):
    client, _op_store, _router, _fake_routewise, _verify_mock = admin_client

    response = await client.get("/admin/routing/provider-routes/minimax-fast", headers=AUTH)

    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy"] == "routewise"
    assert {option["provider"] for option in payload["provider_options"]} >= {
        "chutes",
        "featherless",
        "parasail",
    }
    routes = payload["routes"]
    assert [row["route_id"] for row in routes] == [
        "minimax-fast:chutes-api",
        "minimax-fast:featherless-api",
        "minimax-fast:openrouter[deepinfra]-api",
    ]
    assert routes[0]["provider"] == "chutes"
    assert routes[0]["upstream_provider"] == "chutes"
    assert routes[0]["quota_limit"] == 5000
    assert routes[1]["provider"] == "featherless"
    assert routes[1]["upstream_provider"] == "featherless"
    assert routes[1]["quota_limit"] is None
    assert routes[2]["provider"] == "deepinfra"
    assert routes[2]["upstream_provider"] == "deepinfra"
    assert all(row["strategy"] == "routewise" for row in routes)


@pytest.mark.asyncio
async def test_put_provider_route_updates_upstream_and_preserves_route_semantics(
    admin_client,
):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.list_provider_keys.return_value = [
        ProviderKeyRow(
            id="db-openrouter",
            provider="openrouter",
            key_prefix="openrou...7890",
            label="staging",
            status="active",
            created_at=NOW,
        )
    ]
    op_store.list_provider_route_configs_for_model.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:chutes-api",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 8000,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "openrouter",
            "base_url": "openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 8000,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["provider"] == "chutes"
    assert response.json()["upstream_provider"] == "openrouter"
    assert response.json()["route_type"] == "quota"
    assert response.json()["provider_model_id"] == "minimax/minimax-m2.5"
    assert response.json()["quota_limit"] == 8000
    assert response.json()["api_key"]["source"] == "db"
    verify_mock.assert_awaited_once()
    op_store.upsert_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:chutes-api",
        "openrouter",
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        8000,
        "127.0.0.1",
    )

    updated_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert updated_adapter.config.provider == "openrouter"
    assert updated_adapter.config.openrouter_pinned_provider is None
    assert updated_adapter.config.base_url == "https://openrouter.ai/api/v1"
    assert updated_adapter.config.provider_type == "quota"
    assert updated_adapter.config.route_metadata["provider_type"] == "quota"
    assert updated_adapter.config.route_metadata["route_provider"] == "chutes"
    assert updated_adapter.config.route_metadata["upstream_provider"] == "openrouter"
    assert updated_adapter.config.quota_pool == "chutes-minimax-fast-daily"
    assert updated_adapter.config.quota_source == {
        "provider": "chutes",
        "usage_label": "Daily requests",
        "unit": "requests",
    }
    assert updated_adapter.config.quota == {"limit": 8000}
    assert updated_adapter.config.api_key == "openrouter-db-key-1234567890"
    assert updated_adapter.config.api_keys is None
    fake_routewise._rebuild_from_fixed_router.assert_called_once_with()


@pytest.mark.asyncio
async def test_put_provider_route_verify_failure_does_not_apply(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    verify_mock.side_effect = HTTPException(status_code=400, detail="Provider verification failed")

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
        },
        headers=AUTH,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Provider verification failed"
    op_store.upsert_provider_route_config.assert_not_awaited()
    current_adapter = route_executor.routes["minimax-fast"].raw_adapters[1][0]
    assert current_adapter.config.provider == "featherless"
    fake_routewise._rebuild_from_fixed_router.assert_not_called()


@pytest.mark.asyncio
async def test_put_provider_route_rejects_unsafe_base_url(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "parasail",
            "base_url": "http://localhost:8000/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
        },
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "base_url must use https"
    verify_mock.assert_not_awaited()
    op_store.upsert_provider_route_config.assert_not_awaited()
    current_adapter = route_executor.routes["minimax-fast"].raw_adapters[1][0]
    assert current_adapter.config.provider == "featherless"
    fake_routewise._rebuild_from_fixed_router.assert_not_called()


@pytest.mark.asyncio
async def test_put_provider_route_rejects_private_dns_base_url(admin_client, monkeypatch):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    monkeypatch.setattr(
        provider_routes.socket,
        "getaddrinfo",
        lambda _host, port, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.5", port or 443),
            )
        ],
    )

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "parasail",
            "base_url": "https://evil.example/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
        },
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "base_url host is not allowed"
    verify_mock.assert_not_awaited()
    op_store.upsert_provider_route_config.assert_not_awaited()
    current_adapter = route_executor.routes["minimax-fast"].raw_adapters[1][0]
    assert current_adapter.config.provider == "featherless"
    fake_routewise._rebuild_from_fixed_router.assert_not_called()


@pytest.mark.asyncio
async def test_put_provider_route_rejects_duplicate_route_id(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    duplicate = _compat_adapter(
        provider="chutes",
        endpoint_id="minimax-fast:chutes-api",
        base_url="https://llm2.chutes.ai/v1",
        provider_model_id="MiniMaxAI/MiniMax-M2.5-TEE",
        provider_type="quota",
        quota_pool="chutes-minimax-fast-daily-duplicate",
        quota_source={
            "provider": "chutes",
            "usage_label": "Daily requests duplicate",
            "unit": "requests",
        },
        quota={"limit": 5000},
    )
    route = route_executor.routes["minimax-fast"]
    route.raw_adapters.append((duplicate, 1.0, "minimax-fast:chutes-api"))

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
        },
        headers=AUTH,
    )

    assert response.status_code == 409
    assert "duplicate provider route id" in response.json()["detail"]
    verify_mock.assert_not_awaited()
    op_store.upsert_provider_route_config.assert_not_awaited()
    fake_routewise._rebuild_from_fixed_router.assert_not_called()


@pytest.mark.asyncio
async def test_delete_provider_route_restores_yaml_baseline(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.list_provider_route_configs_for_model.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:featherless-api",
            "provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": None,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    put_response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
        },
        headers=AUTH,
    )
    assert put_response.status_code == 200
    assert route_executor.routes["minimax-fast"].raw_adapters[1][0].config.provider == "openrouter"

    op_store.list_provider_route_configs_for_model.return_value = []
    delete_response = await client.delete(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        headers=AUTH,
    )

    assert delete_response.status_code == 200
    payload = delete_response.json()
    assert payload["route_id"] == "minimax-fast:featherless-api"
    assert payload["source"] == "yaml"
    assert payload["upstream_provider"] == "featherless"
    restored_adapter = route_executor.routes["minimax-fast"].raw_adapters[1][0]
    assert restored_adapter.config.provider == "featherless"
    assert restored_adapter.config.base_url == "https://api.featherless.ai/v1"
    op_store.delete_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:featherless-api",
    )
    verify_mock.assert_awaited_once()
    assert fake_routewise._rebuild_from_fixed_router.call_count == 2


@pytest.mark.asyncio
async def test_apply_persisted_provider_route_config_skips_stale_positional_route_id(
    admin_client,
):
    _client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.list_all_provider_route_configs.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "route-0",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 8000,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    services = AppServices(
        router=route_executor,
        model_router_registry=MagicMock(),
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )

    await apply_persisted_provider_route_configs(services, op_store)

    current_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert current_adapter.config.provider == "chutes"
    fake_routewise._rebuild_from_fixed_router.assert_not_called()
    op_store.delete_provider_route_config.assert_awaited_once_with("minimax-fast", "route-0")
