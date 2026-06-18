"""Tests for admin provider route override endpoints."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from routing.routewise.envelope import EnvelopeNotCalibratedError
from serving.adapters import ModelConfig, OpenAICompatAdapter, dynamic_keys
from serving.servers.deps import AppServices
from serving.servers.registry import _make_adapter
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import provider_routes
from serving.servers.routers.admin.provider_routes import (
    apply_persisted_provider_route_candidates,
    apply_persisted_provider_route_configs,
)
from serving.storage.base import ProviderKeyRow

AUTH = {"Authorization": "Bearer test-admin"}
NOW = datetime(2026, 6, 16, tzinfo=timezone.utc)


class _ManagedTestRouter:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class _FailingManagedTestRouter(_ManagedTestRouter):
    async def start(self) -> None:
        self.started += 1
        raise EnvelopeNotCalibratedError("envelope not calibrated")


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
    op_store.list_provider_route_candidates_for_model = AsyncMock(return_value=[])
    op_store.list_all_provider_route_candidates = AsyncMock(return_value=[])
    op_store.list_settings = AsyncMock(return_value=[])
    op_store.set_setting = AsyncMock()
    op_store.upsert_provider_route_config = AsyncMock()
    op_store.delete_provider_route_config = AsyncMock(return_value=True)
    op_store.upsert_provider_route_candidate = AsyncMock()
    op_store.delete_provider_route_candidate = AsyncMock(return_value=True)
    op_store.delete_provider_route_candidate_with_config = AsyncMock(return_value=True)
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
    fake_routewise.start = AsyncMock()
    fake_routewise.stop = AsyncMock()
    registry = MagicMock()
    strategy_state = {"minimax-fast": "routewise"}
    registry.get_router_name.side_effect = lambda model_id: strategy_state.get(
        model_id,
        "routewise",
    )
    registry.validate_router_strategy = MagicMock()
    registry.get_router = MagicMock(return_value=fake_routewise)
    registry.set_router_override = MagicMock(
        side_effect=lambda model_id, strategy: strategy_state.__setitem__(
            model_id,
            strategy,
        )
    )
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
        "openrouter",
    }
    assert {option["provider"] for option in payload["openrouter_provider_options"]} >= {
        "deepinfra",
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
    assert routes[2]["provider"] == "openrouter"
    assert routes[2]["upstream_provider"] == "openrouter"
    assert routes[2]["openrouter_provider"] == "deepinfra"
    assert all(row["strategy"] == "routewise" for row in routes)


def test_parse_openrouter_provider_options_from_endpoints():
    options = provider_routes._parse_openrouter_provider_options(
        {
            "data": {
                "endpoints": [
                    {"provider_name": "Inceptron", "tag": "inceptron/fp8"},
                    {"provider_name": "AkashML", "tag": "akashml/fp8"},
                    {"provider_name": "DeepInfra", "tag": "deepinfra/fp8"},
                    {"provider_name": "DeepInfra duplicate", "tag": "deepinfra/bf16"},
                    {"provider_name": "Chutes", "tag": "chutes/fp8"},
                ]
            }
        }
    )

    assert [(option.provider, option.label) for option in options] == [
        ("inceptron", "Inceptron"),
        ("akashml", "AkashML"),
        ("deepinfra", "DeepInfra"),
        ("chutes", "Chutes"),
    ]


def test_openrouter_endpoints_url_preserves_model_slug_separator():
    assert provider_routes._openrouter_endpoints_url("minimax/minimax-m2.5") == (
        "https://openrouter.ai/api/v1/models/minimax/minimax-m2.5/endpoints"
    )

    with pytest.raises(HTTPException) as exc_info:
        provider_routes._openrouter_endpoints_url("../minimax")

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_get_openrouter_provider_options_discovers_model_endpoints(
    admin_client,
    monkeypatch,
):
    client, _op_store, _router, _fake_routewise, _verify_mock = admin_client

    async def fake_fetch(model_id: str):
        assert model_id == "minimax/minimax-m2.5"
        return [
            provider_routes.OpenRouterProviderOption(
                provider="inceptron",
                label="Inceptron",
            ),
            provider_routes.OpenRouterProviderOption(
                provider="chutes",
                label="Chutes",
            ),
        ]

    monkeypatch.setattr(provider_routes, "_fetch_openrouter_provider_options", fake_fetch)

    response = await client.get(
        "/admin/routing/openrouter-providers",
        params={"provider_model_id": " minimax/minimax-m2.5 "},
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_model_id"] == "minimax/minimax-m2.5"
    assert [option["provider"] for option in payload["providers"]] == ["inceptron", "chutes"]


@pytest.mark.asyncio
async def test_patch_provider_route_strategy_updates_model_router(admin_client):
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client

    response = await client.patch(
        "/admin/routing/provider-route-strategies/minimax-fast",
        json={"strategy": "fixed"},
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_id"] == "minimax-fast"
    assert payload["strategy"] == "fixed"
    assert {row["strategy"] for row in payload["routes"]} == {"fixed"}
    op_store.set_setting.assert_awaited_once_with(
        "model_router_strategy:minimax-fast",
        "fixed",
        "string",
        "127.0.0.1",
    )


@pytest.mark.asyncio
async def test_patch_provider_route_strategy_does_not_persist_when_apply_fails(
    admin_client,
    monkeypatch,
):
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client

    async def fail_apply(*_args, **_kwargs) -> None:
        raise HTTPException(status_code=409, detail="envelope not calibrated")

    monkeypatch.setattr(provider_routes, "_apply_model_router_strategy", fail_apply)

    response = await client.patch(
        "/admin/routing/provider-route-strategies/minimax-fast",
        json={"strategy": "routewise"},
        headers=AUTH,
    )

    assert response.status_code == 409
    op_store.set_setting.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_model_router_strategy_starts_new_managed_router(admin_client):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    old_router = object()
    new_router = _ManagedTestRouter()
    registry = MagicMock()
    registry.get_router_name.return_value = "fixed"
    registry.validate_router_strategy = MagicMock()
    registry.get_router = MagicMock(return_value=object())
    registry.cached_routers.return_value = []
    registry.set_router_override = MagicMock()
    registry.get_router.side_effect = [old_router, new_router]
    registry.cached_routers.return_value = [new_router]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[],
    )

    await provider_routes._apply_model_router_strategy(
        services,
        "minimax-fast",
        "routewise",
    )

    assert new_router.started == 1
    assert services.managed_routers == [new_router]
    registry.set_router_override.assert_called_once_with("minimax-fast", "routewise")


@pytest.mark.asyncio
async def test_apply_model_router_strategy_rolls_back_when_new_router_cannot_start(
    admin_client,
):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    old_router = _ManagedTestRouter()
    new_router = _FailingManagedTestRouter()
    registry = MagicMock()
    registry.get_router_name.return_value = "fixed"
    registry.get_configured_router_name.return_value = "fixed"
    registry.validate_router_strategy = MagicMock()
    registry.set_router_override = MagicMock()
    registry.get_router.side_effect = [old_router, new_router, old_router]
    registry.cached_routers.return_value = [new_router]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[old_router],
    )

    with pytest.raises(HTTPException) as exc_info:
        await provider_routes._apply_model_router_strategy(
            services,
            "minimax-fast",
            "routewise",
        )

    assert exc_info.value.status_code == 409
    assert registry.set_router_override.call_args_list == [
        call("minimax-fast", "routewise"),
        call("minimax-fast", "fixed"),
    ]
    assert new_router.started == 1
    assert old_router.stopped == 0
    assert services.managed_routers == [old_router]


@pytest.mark.asyncio
async def test_apply_model_router_strategy_stops_removed_managed_router(admin_client):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    old_router = _ManagedTestRouter()
    new_router = object()
    registry = MagicMock()
    registry.get_router_name.return_value = "routewise"
    registry.validate_router_strategy = MagicMock()
    registry.get_router = MagicMock(return_value=object())
    registry.cached_routers.return_value = []
    registry.set_router_override = MagicMock()
    registry.get_router.side_effect = [old_router, new_router]
    registry.cached_routers.return_value = [new_router]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[old_router],
    )

    await provider_routes._apply_model_router_strategy(
        services,
        "minimax-fast",
        "fixed",
    )

    assert old_router.stopped == 1
    assert services.managed_routers == []
    registry.set_router_override.assert_called_once_with("minimax-fast", "fixed")


@pytest.mark.asyncio
async def test_apply_persisted_model_router_strategy_overrides(admin_client):
    from serving.servers.routers.admin.provider_routes import (
        apply_persisted_model_router_strategy_overrides,
    )

    _client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.list_settings.return_value = [
        {
            "key": "model_router_strategy:minimax-fast",
            "value": "fixed",
            "value_type": "string",
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        },
        {
            "key": "routewise_latency_min_samples",
            "value": "12",
            "value_type": "int",
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        },
    ]
    registry = MagicMock()
    registry.get_router_name.return_value = "routewise"
    registry.validate_router_strategy = MagicMock()
    registry.get_router = MagicMock(return_value=object())
    registry.cached_routers.return_value = []
    registry.set_router_override = MagicMock()
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )

    await apply_persisted_model_router_strategy_overrides(services, op_store)

    registry.set_router_override.assert_called_once_with("minimax-fast", "fixed")


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
            "provider": "parasail",
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
            "openrouter_provider": "parasail",
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
    assert response.json()["openrouter_provider"] == "parasail"
    assert response.json()["route_type"] == "quota"
    assert response.json()["provider_model_id"] == "minimax/minimax-m2.5"
    assert response.json()["quota_limit"] == 8000
    assert response.json()["api_key"]["source"] == "db"
    verify_mock.assert_awaited_once()
    op_store.upsert_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:chutes-api",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        8000,
        "127.0.0.1",
    )

    updated_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert updated_adapter.config.provider == "openrouter"
    assert updated_adapter.config.openrouter_pinned_provider == "parasail"
    assert updated_adapter.config.base_url == "https://openrouter.ai/api/v1"
    assert updated_adapter.config.provider_type == "quota"
    assert updated_adapter.config.route_metadata["provider_type"] == "quota"
    assert updated_adapter.config.route_metadata["route_provider"] == "chutes"
    assert updated_adapter.config.route_metadata["upstream_provider"] == "openrouter[parasail]"
    assert updated_adapter.config.quota_pool == "chutes-minimax-fast-daily"
    assert updated_adapter.config.quota_source == {
        "provider": "chutes",
        "usage_label": "Daily requests",
        "unit": "requests",
    }
    assert updated_adapter.config.quota == {"limit": 8000}
    assert updated_adapter.config.api_key is None
    assert updated_adapter.config.api_keys == ["openrouter-db-key-1234567890"]
    assert updated_adapter._key_pool is not None
    dynamic_keys.add_key_to_provider("openrouter", "openrouter-db-key-secondary")
    assert updated_adapter._key_pool.snapshot_keys() == ["openrouter-db-key-1234567890"]
    assert dynamic_keys.remove_key_from_provider("openrouter", "openrouter-db-key-1234567890") == 1
    assert updated_adapter._key_pool.snapshot_keys() == []
    fake_routewise._rebuild_from_fixed_router.assert_called_once_with()


@pytest.mark.asyncio
async def test_put_provider_route_allows_openrouter_pin_matching_route_provider(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    op_store.list_provider_route_configs_for_model.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:chutes-api",
            "provider": "openrouter[chutes]",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 5000,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "openrouter",
            "openrouter_provider": "Chutes",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 5000,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "chutes"
    assert payload["upstream_provider"] == "openrouter"
    assert payload["openrouter_provider"] == "chutes"
    assert payload["quota_limit"] == 5000
    op_store.upsert_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:chutes-api",
        "openrouter[chutes]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        5000,
        "127.0.0.1",
    )

    updated_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert updated_adapter.config.provider == "openrouter"
    assert updated_adapter.config.openrouter_pinned_provider == "chutes"
    assert updated_adapter.config.route_metadata["route_provider"] == "chutes"
    assert updated_adapter.config.route_metadata["upstream_provider"] == "openrouter[chutes]"
    assert updated_adapter.config.quota == {"limit": 5000}
    verify_mock.assert_awaited_once()
    fake_routewise._rebuild_from_fixed_router.assert_called_once_with()


@pytest.mark.asyncio
async def test_verify_provider_route_update_does_not_apply(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")

    response = await client.post(
        "/admin/routing/provider-route-verifications/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 8000,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    verify_mock.assert_awaited_once()
    op_store.upsert_provider_route_config.assert_not_awaited()
    current_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert current_adapter.config.provider == "chutes"
    assert current_adapter.config.quota == {"limit": 5000}
    fake_routewise._rebuild_from_fixed_router.assert_not_called()


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
async def test_post_provider_route_candidate_adds_runtime_route(admin_client):
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

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "weight": 2.5,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "runtime"
    assert payload["route_id"] == "minimax-fast:openrouter[parasail]-api"
    assert payload["route_type"] == "on_demand"
    assert payload["upstream_provider"] == "openrouter"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["effective_weight"] == 2.5
    op_store.upsert_provider_route_candidate.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:openrouter[parasail]-api",
        "on_demand",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        None,
        None,
        2.5,
        "127.0.0.1",
    )
    verify_mock.assert_awaited_once()

    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.provider == "openrouter"
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"
    assert runtime_adapter.config.route_metadata["runtime_candidate"] is True
    assert runtime_adapter.config.route_metadata["route_provider"] == "openrouter[parasail]"
    fake_routewise._rebuild_from_fixed_router.assert_called_once_with()


@pytest.mark.asyncio
async def test_verify_provider_route_candidate_does_not_add_runtime_route(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")

    response = await client.post(
        "/admin/routing/provider-route-candidate-verifications/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "weight": 2.5,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    verify_mock.assert_awaited_once()
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    assert len(route_executor.routes["minimax-fast"].raw_adapters) == 3
    fake_routewise._rebuild_from_fixed_router.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_provider_route_candidate_cannot_be_overridden(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")

    create_response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "weight": 1,
        },
        headers=AUTH,
    )
    assert create_response.status_code == 200
    runtime_route_id = "minimax-fast:openrouter[parasail]-api"
    assert (
        route_executor.routes["minimax-fast"]
        .raw_adapters[-1][0]
        .config.route_metadata["runtime_candidate"]
    )

    update_response = await client.put(
        f"/admin/routing/provider-routes/minimax-fast/{runtime_route_id}",
        json={
            "upstream_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
        },
        headers=AUTH,
    )
    restore_response = await client.delete(
        f"/admin/routing/provider-routes/minimax-fast/{runtime_route_id}",
        headers=AUTH,
    )

    assert update_response.status_code == 400
    assert update_response.json()["detail"] == "Runtime-added provider routes cannot be overridden"
    assert restore_response.status_code == 400
    assert (
        restore_response.json()["detail"]
        == "Runtime-added provider routes must be deleted as candidates"
    )
    op_store.upsert_provider_route_config.assert_not_awaited()
    op_store.delete_provider_route_config.assert_not_awaited()
    assert fake_routewise._rebuild_from_fixed_router.call_count == 1


@pytest.mark.asyncio
async def test_post_provider_route_candidate_adds_openrouter_sort_policy(admin_client):
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

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_sort": "throughput",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "weight": 1,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["route_id"] == "minimax-fast:openrouter-api"
    assert payload["upstream_provider"] == "openrouter"
    assert payload["openrouter_provider"] is None
    assert payload["openrouter_sort"] == "throughput"
    op_store.upsert_provider_route_candidate.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:openrouter-api",
        "on_demand",
        "openrouter",
        "throughput",
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        None,
        None,
        1.0,
        "127.0.0.1",
    )
    verify_mock.assert_awaited_once()

    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.provider == "openrouter"
    assert runtime_adapter.config.openrouter_sort == "throughput"
    assert runtime_adapter.config.openrouter_pinned_provider is None
    assert runtime_adapter.config.route_metadata["openrouter_sort"] == "throughput"
    fake_routewise._rebuild_from_fixed_router.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_provider_route_candidate_rejects_route_type_provider_mismatch(admin_client):
    client, op_store, _route_executor, fake_routewise, verify_mock = admin_client

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "chutes",
            "base_url": "https://llm.chutes.ai/v1",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5-TEE",
            "weight": 1,
        },
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "chutes can only be added as quota"
    verify_mock.assert_not_awaited()
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    fake_routewise._rebuild_from_fixed_router.assert_not_called()


@pytest.mark.asyncio
async def test_delete_provider_route_candidate_removes_runtime_route(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")

    create_response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "weight": 1,
        },
        headers=AUTH,
    )
    assert create_response.status_code == 200
    assert len(route_executor.routes["minimax-fast"].raw_adapters) == 4

    response = await client.delete(
        "/admin/routing/provider-route-candidates/minimax-fast/"
        "minimax-fast:openrouter[parasail]-api",
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert [row["route_id"] for row in payload["routes"]] == [
        "minimax-fast:chutes-api",
        "minimax-fast:featherless-api",
        "minimax-fast:openrouter[deepinfra]-api",
    ]
    op_store.delete_provider_route_candidate_with_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:openrouter[parasail]-api",
    )
    op_store.delete_provider_route_candidate.assert_not_awaited()
    op_store.delete_provider_route_config.assert_not_awaited()
    assert len(route_executor.routes["minimax-fast"].raw_adapters) == 3
    assert fake_routewise._rebuild_from_fixed_router.call_count == 2


@pytest.mark.asyncio
async def test_delete_provider_route_candidate_rejects_config_route(admin_client):
    client, op_store, _route_executor, fake_routewise, _verify_mock = admin_client

    response = await client.delete(
        "/admin/routing/provider-route-candidates/minimax-fast/minimax-fast:featherless-api",
        headers=AUTH,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only runtime-added provider routes can be deleted"
    op_store.delete_provider_route_candidate.assert_not_awaited()
    op_store.delete_provider_route_candidate_with_config.assert_not_awaited()
    fake_routewise._rebuild_from_fixed_router.assert_not_called()


@pytest.mark.asyncio
async def test_apply_persisted_provider_route_candidates(admin_client):
    _client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.list_all_provider_route_candidates.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:openrouter[parasail]-api",
            "route_type": "on_demand",
            "provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": None,
            "concurrency_limit": None,
            "weight": 1.5,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]
    registry = MagicMock()
    registry.cached_routers.return_value = [fake_routewise]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )

    await apply_persisted_provider_route_candidates(services, op_store)

    runtime_adapter, raw_weight, endpoint_id = route_executor.routes["minimax-fast"].raw_adapters[
        -1
    ]
    assert endpoint_id == "minimax-fast:openrouter[parasail]-api"
    assert raw_weight == 1.5
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"
    assert runtime_adapter.config.route_metadata["runtime_candidate"] is True
    fake_routewise._rebuild_from_fixed_router.assert_called_once_with()


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
