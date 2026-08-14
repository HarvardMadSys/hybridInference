"""Tests for admin provider route override endpoints."""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from routing.model_router_registry import ModelRouterRegistry, StaleRouterStrategyChangeError
from routing.routewise.envelope import EnvelopeNotCalibratedError
from routing.routewise.router import RouteWiseRouter
from serving.adapters import ModelConfig, OpenAICompatAdapter, dynamic_keys, provider_registry
from serving.adapters.provider_registry import RuntimeProviderDefinition
from serving.config.routewise_model_settings import model_routewise_setting_keys
from serving.pricing import PricingSchedule
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
RUNTIME_PRICING = {
    "prompt": "0.14",
    "completion": "0.28",
    "input_cache_reads": "0.0028",
    "input_cache_writes": "0",
}
OPENROUTER_DEEPINFRA_PRICING = {
    "prompt": "0.15",
    "completion": "1.15",
    "image": "0",
    "request": "0",
    "input_cache_reads": "0.03",
    "input_cache_writes": "0",
}
OPENROUTER_PARASAIL_PRICING = {
    "prompt": "0.3",
    "completion": "1.2",
    "image": "0",
    "request": "0",
    "input_cache_reads": "0.03",
    "input_cache_writes": "0",
}
OPENROUTER_MINIMAX_PRICING = {
    "prompt": "0.3",
    "completion": "1.2",
    "image": "0",
    "request": "0",
    "input_cache_reads": "0.03",
    "input_cache_writes": "0",
}
OPENROUTER_HIGHSPEED_PRICING = {
    "prompt": "0.6",
    "completion": "2.4",
    "image": "0",
    "request": "0",
    "input_cache_reads": "0.06",
    "input_cache_writes": "0",
}
OPENROUTER_ENDPOINT_PAYLOAD = {
    "data": {
        "endpoints": [
            {
                "provider_name": "DeepInfra",
                "tag": "deepinfra/fp8",
                "pricing": {
                    "prompt": "0.00000015",
                    "completion": "0.00000115",
                    "input_cache_read": "0.00000003",
                },
            },
            {
                "provider_name": "Parasail",
                "tag": "parasail/fp8",
                "pricing": {
                    "prompt": "0.0000003",
                    "completion": "0.0000012",
                    "input_cache_read": "0.00000003",
                },
            },
            {
                "provider_name": "MiniMax",
                "tag": "minimax/fp8",
                "pricing": {
                    "prompt": "0.0000003",
                    "completion": "0.0000012",
                    "input_cache_read": "0.00000003",
                },
            },
            {
                "provider_name": "MiniMax",
                "tag": "minimax/highspeed",
                "pricing": {
                    "prompt": "0.0000006",
                    "completion": "0.0000024",
                    "input_cache_read": "0.00000006",
                },
            },
        ]
    }
}


class _ManagedTestRouter:
    def __init__(self, events: list[str] | None = None, name: str = "router") -> None:
        self.started = 0
        self.stopped = 0
        self.events = events
        self.name = name

    async def start(self) -> None:
        self.started += 1
        if self.events is not None:
            self.events.append(f"{self.name}.start")

    async def stop(self) -> None:
        self.stopped += 1
        if self.events is not None:
            self.events.append(f"{self.name}.stop")


def test_routewise_rebuild_delegates_to_registry_capability() -> None:
    refresh_route_tables = MagicMock()
    registry = SimpleNamespace(refresh_route_tables=refresh_route_tables)
    services = SimpleNamespace(model_router_registry=registry)

    provider_routes._rebuild_routewise_routers(services)

    refresh_route_tables.assert_called_once_with()


class _FailingManagedTestRouter(_ManagedTestRouter):
    async def start(self) -> None:
        await super().start()
        raise EnvelopeNotCalibratedError("envelope not calibrated")


class _StopFailingManagedTestRouter(_ManagedTestRouter):
    async def stop(self) -> None:
        await super().stop()
        raise RuntimeError("stop failed")


class _UnstoppableFailingManagedTestRouter(_FailingManagedTestRouter):
    async def stop(self) -> None:
        await super().stop()
        raise RuntimeError("stop failed")


class _HangingManagedTestRouter(_ManagedTestRouter):
    async def stop(self) -> None:
        self.stopped += 1
        await asyncio.Event().wait()


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


def _register_on_demand_only_route(route_executor, model_id: str = "on-demand-only") -> None:
    adapter = _compat_adapter(
        model_id=model_id,
        provider="openrouter",
        endpoint_id=f"{model_id}:openrouter-api",
        base_url="https://openrouter.ai/api/v1",
        provider_model_id="openai/gpt-oss-20b",
    )
    route_executor.register_route(model_id, [(adapter, 1.0)])


@pytest.fixture
async def admin_client(monkeypatch):
    dynamic_keys.reset()

    op_store = MagicMock()
    op_store.list_provider_route_configs_for_model = AsyncMock(return_value=[])
    op_store.list_all_provider_route_configs = AsyncMock(return_value=[])
    op_store.list_provider_route_candidates_for_model = AsyncMock(return_value=[])
    op_store.list_all_provider_route_candidates = AsyncMock(return_value=[])
    op_store.list_settings = AsyncMock(return_value=[])
    op_store.get_setting = AsyncMock(return_value=None)
    op_store.set_setting = AsyncMock()
    op_store.delete_setting = AsyncMock(return_value=True)
    op_store.get_model_visibility_override = AsyncMock(return_value=None)
    op_store.set_model_visibility_override = AsyncMock()
    op_store.delete_model_visibility_override = AsyncMock(return_value=True)
    op_store.get_model_concurrency_exemption = AsyncMock(return_value=None)
    op_store.set_model_concurrency_exemption = AsyncMock()
    op_store.delete_model_concurrency_exemption = AsyncMock(return_value=True)
    op_store.list_weight_overrides_for_model = AsyncMock(return_value=[])
    op_store.upsert_weight_override = AsyncMock()
    op_store.delete_weight_override = AsyncMock(return_value=True)
    op_store.upsert_provider_route_config = AsyncMock()
    op_store.delete_provider_route_config = AsyncMock(return_value=True)
    op_store.upsert_provider_route_candidate = AsyncMock()
    op_store.delete_provider_route_candidate = AsyncMock(return_value=True)
    op_store.delete_provider_route_candidate_with_config = AsyncMock(return_value=True)
    op_store.delete_runtime_model_state = AsyncMock(return_value=True)
    op_store.get_provider_key_full = AsyncMock(return_value=None)
    op_store.list_provider_keys = AsyncMock(return_value=[])
    op_store.list_provider_keys_full = AsyncMock(return_value=[])
    op_store.list_disabled_provider_env_key_hashes = AsyncMock(return_value=set())
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

    fake_routewise = MagicMock(spec=RouteWiseRouter)
    fake_routewise.refresh_route_table = MagicMock()
    fake_routewise.start = AsyncMock()
    fake_routewise.stop = AsyncMock()
    fake_routewise.quota_pools = {}
    registry = MagicMock()
    strategy_state = {"minimax-fast": "routewise"}
    router_cache = {}
    registry.get_router_name.side_effect = lambda model_id: strategy_state.get(
        model_id,
        "routewise",
    )
    registry.validate_router_strategy = MagicMock()

    def get_router(model_id):
        strategy = strategy_state.get(model_id, "routewise")
        expected = route_executor if strategy == "fixed" else fake_routewise
        if router_cache.get(model_id) is not expected:
            router_cache[model_id] = expected
        return router_cache[model_id]

    def prepare_router_strategy_change(model_id, strategy):
        previous_router = get_router(model_id)
        effective_strategy = strategy or "routewise"
        candidate = route_executor if effective_strategy == "fixed" else fake_routewise
        return SimpleNamespace(
            canonical_model_id=model_id,
            strategy=effective_strategy,
            previous_router=previous_router,
            router=candidate,
        )

    def commit_router_strategy_change(change):
        strategy_state[change.canonical_model_id] = change.strategy
        router_cache[change.canonical_model_id] = change.router
        return change.router

    def set_router_override(model_id, strategy):
        commit_router_strategy_change(prepare_router_strategy_change(model_id, strategy))

    def clear_router_override(model_id):
        strategy_state.pop(model_id, None)
        router_cache.pop(model_id, None)

    registry.get_router = MagicMock(side_effect=get_router)
    registry.prepare_router_strategy_change = MagicMock(side_effect=prepare_router_strategy_change)
    registry.commit_router_strategy_change = MagicMock(side_effect=commit_router_strategy_change)
    registry.set_router_override = MagicMock(side_effect=set_router_override)
    registry.clear_router_override = MagicMock(side_effect=clear_router_override)
    registry.refresh_route_tables = MagicMock(
        side_effect=fake_routewise.refresh_route_table,
    )
    registry.cached_routers.side_effect = lambda: list(router_cache.values())

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

    async def fake_openrouter_endpoint_payload(_model_id: str):
        return OPENROUTER_ENDPOINT_PAYLOAD

    monkeypatch.setattr(
        provider_routes,
        "_fetch_openrouter_endpoint_payload",
        fake_openrouter_endpoint_payload,
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
        client.app = app  # type: ignore[attr-defined]
        yield client, op_store, route_executor, fake_routewise, verify_mock

    dynamic_keys.reset()


@pytest.mark.asyncio
async def test_get_provider_routes_lists_routewise_candidates(admin_client):
    client, _op_store, _router, fake_routewise, _verify_mock = admin_client
    quota_reset_at = datetime(2026, 6, 17, tzinfo=timezone.utc)
    fake_routewise.quota_pools = {
        "chutes-minimax-fast-daily": MagicMock(
            ready=True,
            limit=5000,
            effective_used=123.0,
            remaining=4877,
            reset_at=quota_reset_at,
        )
    }

    response = await client.get("/admin/routing/provider-routes/minimax-fast", headers=AUTH)

    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy"] == "routewise"
    assert {option["provider"] for option in payload["provider_options"]} >= {
        "chutes",
        "featherless",
        "minimax",
        "openrouter",
    }
    minimax_option = next(
        option for option in payload["provider_options"] if option["provider"] == "minimax"
    )
    assert minimax_option["default_base_url"] == "https://api.minimax.io/v1"
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
    assert routes[0]["quota_current_limit"] == 5000
    assert routes[0]["quota_used"] == 123.0
    assert routes[0]["quota_remaining"] == 4877
    assert routes[0]["quota_reset_at"] == quota_reset_at.isoformat().replace("+00:00", "Z")
    assert routes[1]["provider"] == "featherless"
    assert routes[1]["upstream_provider"] == "featherless"
    assert routes[1]["quota_limit"] is None
    assert routes[1]["concurrency_limit"] == 1
    assert routes[2]["provider"] == "openrouter"
    assert routes[2]["upstream_provider"] == "openrouter"
    assert routes[2]["openrouter_provider"] == "deepinfra"
    assert routes[2]["concurrency_limit"] is None
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
                    {"provider_name": "Minimax", "tag": "minimax/fp8"},
                    {"provider_name": "Minimax", "tag": "minimax/highspeed"},
                ]
            }
        }
    )

    assert [(option.provider, option.label) for option in options] == [
        ("inceptron/fp8", "Inceptron Fp8"),
        ("akashml/fp8", "AkashML Fp8"),
        ("deepinfra/fp8", "DeepInfra Fp8"),
        ("deepinfra/bf16", "DeepInfra Bf16"),
        ("chutes/fp8", "Chutes Fp8"),
        ("minimax/fp8", "MiniMax Fp8"),
        ("minimax/highspeed", "MiniMax Highspeed"),
    ]


def test_parse_openrouter_endpoint_pricing_from_endpoints():
    deepinfra = provider_routes._parse_openrouter_endpoint_pricing(
        OPENROUTER_ENDPOINT_PAYLOAD,
        "deepinfra",
    )
    minimax = provider_routes._parse_openrouter_endpoint_pricing(
        OPENROUTER_ENDPOINT_PAYLOAD,
        "minimax",
    )
    highspeed = provider_routes._parse_openrouter_endpoint_pricing(
        OPENROUTER_ENDPOINT_PAYLOAD,
        "minimax/highspeed",
    )

    assert deepinfra is not None
    assert deepinfra.provider == "deepinfra/fp8"
    assert deepinfra.pricing == OPENROUTER_DEEPINFRA_PRICING
    assert minimax is not None
    assert minimax.provider == "minimax/fp8"
    assert minimax.pricing == OPENROUTER_MINIMAX_PRICING
    assert highspeed is not None
    assert highspeed.provider == "minimax/highspeed"
    assert highspeed.pricing == OPENROUTER_HIGHSPEED_PRICING


def test_config_clone_preserves_validated_pricing_schedule():
    config = ModelConfig(
        id="scheduled-model",
        name="Scheduled model",
        provider="deepseek",
        base_url="https://api.deepseek.com",
        pricing={"prompt": "0.14", "completion": "0.28"},
        pricing_schedule={
            "effective_at": "2026-08-16T16:00:00Z",
            "timezone": "UTC",
            "default": {"prompt": "0.22", "completion": "0.66"},
            "windows": [],
        },
    )

    cloned_values = provider_routes._config_to_dict(config)
    cloned = ModelConfig(**cloned_values)

    assert cloned.pricing_schedule is config.pricing_schedule


@pytest.mark.asyncio
async def test_openrouter_endpoint_price_clears_inherited_schedule(monkeypatch):
    endpoint_pricing = provider_routes.OpenRouterEndpointPricing(
        provider="deepinfra/fp8",
        pricing=OPENROUTER_DEEPINFRA_PRICING,
    )
    monkeypatch.setattr(
        provider_routes,
        "_openrouter_pricing_for_target",
        AsyncMock(return_value=endpoint_pricing),
    )
    cfg = {
        "pricing": RUNTIME_PRICING,
        "pricing_schedule": {"inherited": True},
        "route_metadata": {},
    }

    await provider_routes._apply_openrouter_endpoint_pricing(
        cfg,
        provider_model_id="provider/model",
        target=SimpleNamespace(kind="openrouter[deepinfra/fp8]"),
        openrouter_sort=None,
    )

    assert cfg["pricing"] == OPENROUTER_DEEPINFRA_PRICING
    assert cfg["pricing_schedule"] is None


@pytest.mark.asyncio
async def test_openrouter_pricing_failure_drops_inherited_schedule(monkeypatch):
    monkeypatch.setattr(
        provider_routes,
        "_openrouter_pricing_for_target",
        AsyncMock(return_value=None),
    )
    cfg = {
        "pricing": OPENROUTER_DEEPINFRA_PRICING,
        "pricing_schedule": {"inherited": True},
        "route_metadata": {
            "pricing_source": "openrouter_endpoint",
            "pricing_provider": "deepinfra/fp8",
        },
    }

    await provider_routes._apply_openrouter_endpoint_pricing(
        cfg,
        provider_model_id="provider/model",
        target=SimpleNamespace(kind="openrouter[deepinfra/fp8]"),
        openrouter_sort=None,
    )

    # Withdrawing the price has to withdraw the schedule with it: without
    # ``pricing`` the route falls back to ModelConfig's all-zero default, and a
    # surviving schedule would layer real per-token prices back onto those zeros.
    assert "pricing" not in cfg
    assert "pricing_schedule" not in cfg
    assert "pricing_source" not in cfg["route_metadata"]


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
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)

    response = await client.patch(
        "/admin/routing/provider-route-strategies/on-demand-only",
        json={"strategy": "fixed"},
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_id"] == "on-demand-only"
    assert payload["strategy"] == "fixed"
    assert {row["strategy"] for row in payload["routes"]} == {"fixed"}
    op_store.set_setting.assert_awaited_once_with(
        "model_router_strategy:on-demand-only",
        "fixed",
        "string",
        "127.0.0.1",
    )


@pytest.mark.asyncio
async def test_patch_provider_route_strategy_rejects_fixed_with_resource_routes(
    admin_client,
):
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client

    response = await client.patch(
        "/admin/routing/provider-route-strategies/minimax-fast",
        json={"strategy": "fixed"},
        headers=AUTH,
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "fixed strategy cannot be used while model has concurrency, quota routes"
    )
    op_store.set_setting.assert_not_awaited()


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
async def test_concurrent_strategy_patches_serialize_persistence_and_live_publish(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)
    services = client.app.state.services
    registry = services.model_router_registry
    strategy_key = "model_router_strategy:on-demand-only"
    persisted: dict[str, dict[str, str]] = {}
    first_persist_entered = asyncio.Event()
    release_first_persist = asyncio.Event()
    second_validated = asyncio.Event()

    async def get_setting(key: str):
        return persisted.get(key)

    async def set_setting(key: str, value: str, value_type: str, updated_by: str):
        if value == "fixed" and not first_persist_entered.is_set():
            first_persist_entered.set()
            await release_first_persist.wait()
        persisted[key] = {
            "key": key,
            "value": value,
            "value_type": value_type,
            "updated_by": updated_by,
        }

    op_store.get_setting.side_effect = get_setting
    op_store.set_setting.side_effect = set_setting

    def signal_second_validation(_model_id: str, strategy: str) -> None:
        if strategy == "routewise":
            second_validated.set()

    registry.validate_router_strategy.side_effect = signal_second_validation

    first = asyncio.create_task(
        client.patch(
            "/admin/routing/provider-route-strategies/on-demand-only",
            json={"strategy": "fixed"},
            headers=AUTH,
        )
    )
    await first_persist_entered.wait()
    second = asyncio.create_task(
        client.patch(
            "/admin/routing/provider-route-strategies/on-demand-only",
            json={"strategy": "routewise"},
            headers=AUTH,
        )
    )
    await second_validated.wait()

    assert op_store.set_setting.await_count == 1
    assert registry.get_router_name("on-demand-only") == "routewise"

    release_first_persist.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text
    assert persisted[strategy_key]["value"] == "routewise"
    assert registry.get_router_name("on-demand-only") == "routewise"
    assert registry.get_router("on-demand-only") is _fake_routewise


@pytest.mark.asyncio
async def test_strategy_patch_cancellation_waits_for_publish_then_propagates(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)
    services = client.app.state.services
    registry = services.model_router_registry
    audit = provider_routes.log_admin_action
    strategy_key = "model_router_strategy:on-demand-only"
    persist_entered = asyncio.Event()
    release_persist = asyncio.Event()
    published = asyncio.Event()
    persisted: dict[str, str] = {}

    async def set_setting(key: str, value: str, _value_type: str, _updated_by: str):
        persist_entered.set()
        await release_persist.wait()
        persisted[key] = value

    original_commit = registry.commit_router_strategy_change.side_effect

    def commit_and_signal(change):
        result = original_commit(change)
        published.set()
        return result

    op_store.set_setting.side_effect = set_setting
    registry.commit_router_strategy_change.side_effect = commit_and_signal

    request = asyncio.create_task(
        client.patch(
            "/admin/routing/provider-route-strategies/on-demand-only",
            json={"strategy": "fixed"},
            headers=AUTH,
        )
    )
    await persist_entered.wait()
    request.cancel()
    await asyncio.sleep(0)

    assert not request.done()
    registry.commit_router_strategy_change.assert_not_called()

    release_persist.set()
    await published.wait()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert persisted[strategy_key] == "fixed"
    assert registry.get_router_name("on-demand-only") == "fixed"
    assert registry.get_router("on-demand-only") is route_executor
    audit.assert_awaited_once()
    assert audit.await_args.args[2] == "routing.provider_routes.strategy.update"


@pytest.mark.asyncio
async def test_stale_strategy_commit_restores_exact_setting_and_cleans_candidate(admin_client):
    _client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)
    candidate = _ManagedTestRouter()
    change = SimpleNamespace(previous_router=route_executor, router=candidate)
    registry = MagicMock()
    registry.get_router_name.return_value = "fixed"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change
    registry.commit_router_strategy_change.side_effect = StaleRouterStrategyChangeError(
        "stale change"
    )
    registry.cached_routers.return_value = [route_executor]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        operational_store=op_store,
    )
    previous_setting = {
        "key": "model_router_strategy:on-demand-only",
        "value": "fixed",
        "value_type": "legacy-string",
        "updated_by": "original-admin",
    }
    op_store.get_setting.return_value = previous_setting

    with pytest.raises(HTTPException) as exc_info:
        await provider_routes.update_provider_route_strategy(
            "on-demand-only",
            SimpleNamespace(strategy="routewise"),
            admin_id="new-admin",
            services=services,
            op_store=op_store,
        )

    assert exc_info.value.status_code == 409
    assert candidate.started == 1
    assert candidate.stopped == 1
    assert services.managed_routers == []
    assert op_store.set_setting.await_args_list == [
        call(
            "model_router_strategy:on-demand-only",
            "routewise",
            "string",
            "new-admin",
        ),
        call(
            "model_router_strategy:on-demand-only",
            "fixed",
            "legacy-string",
            "original-admin",
        ),
    ]


@pytest.mark.asyncio
async def test_apply_model_router_strategy_starts_new_managed_router(admin_client):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    events: list[str] = []
    old_router = _ManagedTestRouter(events, "old")
    new_router = _ManagedTestRouter(events, "candidate")
    change = SimpleNamespace(previous_router=old_router, router=new_router)
    registry = MagicMock()
    registry.get_router_name.return_value = "fixed"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change
    registry.commit_router_strategy_change.side_effect = lambda _change: events.append("commit")
    registry.cached_routers.return_value = [new_router]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[old_router],
    )

    await provider_routes._apply_model_router_strategy(
        services,
        "minimax-fast",
        "routewise",
    )

    assert new_router.started == 1
    assert old_router.stopped == 1
    assert services.managed_routers == [new_router]
    assert events == ["candidate.start", "commit", "old.stop"]
    registry.prepare_router_strategy_change.assert_called_once_with("minimax-fast", "routewise")
    registry.commit_router_strategy_change.assert_called_once_with(change)


@pytest.mark.asyncio
async def test_apply_model_router_strategy_boot_swaps_without_starting(admin_client):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    events: list[str] = []
    old_router = _ManagedTestRouter(events, "old")
    new_router = _ManagedTestRouter(events, "candidate")
    change = SimpleNamespace(previous_router=old_router, router=new_router)
    registry = MagicMock()
    registry.get_router_name.return_value = "fixed"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change
    registry.commit_router_strategy_change.side_effect = lambda _change: events.append("commit")
    registry.cached_routers.return_value = [new_router]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[old_router],
    )

    await provider_routes._apply_model_router_strategy(
        services,
        "minimax-fast",
        "routewise",
        start_managed=False,
    )

    assert events == ["commit"]
    assert new_router.started == 0
    assert old_router.stopped == 0
    assert services.managed_routers == [new_router]


@pytest.mark.asyncio
async def test_apply_model_router_strategy_applies_settings_before_routewise_start(
    admin_client,
    monkeypatch,
):
    _client, op_store, route_executor, routewise_router, _verify_mock = admin_client
    events: list[str] = []
    old_router = object()
    routewise_router.attach_operational_store.side_effect = lambda _store: events.append("attach")
    routewise_router.start.side_effect = lambda: events.append("start")
    change = SimpleNamespace(previous_router=old_router, router=routewise_router)
    registry = MagicMock()
    registry.get_router_name.return_value = "fixed"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change
    registry.commit_router_strategy_change.side_effect = lambda _change: events.append("commit")
    registry.cached_routers.return_value = [routewise_router]
    settings_resolver = MagicMock()
    apply_settings = AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("settings"))
    monkeypatch.setattr(
        provider_routes,
        "apply_routewise_settings_to_router",
        apply_settings,
    )
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[],
        operational_store=op_store,
        routewise_settings_resolver=settings_resolver,
    )

    await provider_routes._apply_model_router_strategy(
        services,
        "minimax-fast",
        "routewise",
    )

    assert events == ["attach", "settings", "start", "commit"]
    routewise_router.attach_operational_store.assert_called_once_with(op_store)
    apply_settings.assert_awaited_once_with(
        settings_resolver,
        registry,
        "minimax-fast",
        routewise_router,
        refresh_probe_task=False,
    )
    assert services.managed_routers == [routewise_router]


@pytest.mark.asyncio
async def test_apply_model_router_strategy_rolls_back_when_new_router_cannot_start(
    admin_client,
):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    events: list[str] = []
    old_router = _ManagedTestRouter(events, "old")
    new_router = _FailingManagedTestRouter(events, "candidate")
    change = SimpleNamespace(previous_router=old_router, router=new_router)
    registry = MagicMock()
    registry.get_router_name.return_value = "fixed"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change
    registry.cached_routers.return_value = [old_router]
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
    registry.commit_router_strategy_change.assert_not_called()
    assert new_router.started == 1
    assert new_router.stopped == 1
    assert old_router.stopped == 0
    assert services.managed_routers == [old_router]
    assert events == ["candidate.start", "candidate.stop"]


@pytest.mark.asyncio
async def test_apply_model_router_strategy_cleans_candidate_when_commit_is_stale(
    admin_client,
):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    events: list[str] = []
    old_router = _ManagedTestRouter(events, "old")
    new_router = _ManagedTestRouter(events, "candidate")
    change = SimpleNamespace(previous_router=old_router, router=new_router)
    registry = MagicMock()
    registry.get_router_name.return_value = "fixed"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change

    def fail_commit(_change):
        events.append("commit")
        raise RuntimeError("stale change")

    registry.commit_router_strategy_change.side_effect = fail_commit
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[old_router],
    )

    with pytest.raises(RuntimeError, match="stale change"):
        await provider_routes._apply_model_router_strategy(
            services,
            "minimax-fast",
            "routewise",
        )

    assert events == ["candidate.start", "commit", "candidate.stop"]
    assert services.managed_routers == [old_router]


@pytest.mark.asyncio
async def test_apply_model_router_strategy_tracks_candidate_when_cleanup_fails(admin_client):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    old_router = _ManagedTestRouter()
    new_router = _UnstoppableFailingManagedTestRouter()
    change = SimpleNamespace(previous_router=old_router, router=new_router)
    registry = MagicMock()
    registry.get_router_name.return_value = "fixed"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change
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
    registry.commit_router_strategy_change.assert_not_called()
    assert services.managed_routers == [old_router, new_router]


@pytest.mark.asyncio
async def test_apply_model_router_strategy_stops_removed_managed_router(admin_client):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)
    events: list[str] = []
    old_router = _ManagedTestRouter(events, "old")
    new_router = object()
    change = SimpleNamespace(previous_router=old_router, router=new_router)
    registry = MagicMock()
    registry.get_router_name.return_value = "routewise"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change
    registry.commit_router_strategy_change.side_effect = lambda _change: events.append("commit")
    registry.cached_routers.return_value = [new_router]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[old_router],
    )

    await provider_routes._apply_model_router_strategy(
        services,
        "on-demand-only",
        "fixed",
    )

    assert old_router.stopped == 1
    assert services.managed_routers == []
    assert events == ["commit", "old.stop"]


@pytest.mark.asyncio
async def test_apply_model_router_strategy_keeps_old_router_when_stop_fails(admin_client):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)
    events: list[str] = []
    old_router = _StopFailingManagedTestRouter(events, "old")
    new_router = object()
    change = SimpleNamespace(previous_router=old_router, router=new_router)
    registry = MagicMock()
    registry.get_router_name.return_value = "routewise"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change
    registry.commit_router_strategy_change.side_effect = lambda _change: events.append("commit")
    registry.cached_routers.return_value = [new_router]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[old_router],
    )

    await provider_routes._apply_model_router_strategy(
        services,
        "on-demand-only",
        "fixed",
    )

    assert events == ["commit", "old.stop"]
    assert services.managed_routers == [old_router]
    registry.commit_router_strategy_change.assert_called_once_with(change)


@pytest.mark.asyncio
async def test_apply_model_router_strategy_bounds_hanging_old_router_stop(
    admin_client,
    monkeypatch,
):
    _client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)
    old_router = _HangingManagedTestRouter()
    new_router = object()
    change = SimpleNamespace(previous_router=old_router, router=new_router)
    registry = MagicMock()
    registry.get_router_name.return_value = "routewise"
    registry.validate_router_strategy = MagicMock()
    registry.prepare_router_strategy_change.return_value = change
    registry.cached_routers.return_value = [new_router]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[old_router],
    )
    monkeypatch.setattr(provider_routes, "MANAGED_ROUTER_STOP_TIMEOUT_SEC", 0.001)

    await asyncio.wait_for(
        provider_routes._apply_model_router_strategy(
            services,
            "on-demand-only",
            "fixed",
        ),
        timeout=0.5,
    )

    registry.commit_router_strategy_change.assert_called_once_with(change)
    assert old_router.stopped == 1
    assert services.managed_routers == [old_router]


@pytest.mark.asyncio
async def test_apply_model_router_strategy_integrates_real_registry(admin_client):
    _client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)
    registry = ModelRouterRegistry(
        models_config={"on-demand-only": {"router": "fixed"}},
        shared_fixed_router=route_executor,
    )
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        operational_store=op_store,
    )

    await provider_routes._apply_model_router_strategy(
        services,
        "on-demand-only",
        "routewise",
    )

    routewise_router = registry.get_router("on-demand-only")
    assert isinstance(routewise_router, RouteWiseRouter)
    assert registry.get_router_override("on-demand-only") == "routewise"
    assert services.managed_routers == [routewise_router]
    assert not hasattr(routewise_router, "_model_router_override_id")
    assert not hasattr(routewise_router, "_model_router_fallback_strategy")

    await provider_routes._apply_model_router_strategy(
        services,
        "on-demand-only",
        "fixed",
    )

    assert registry.get_router("on-demand-only") is route_executor
    assert registry.get_router_override("on-demand-only") is None
    assert services.managed_routers == []


@pytest.mark.asyncio
async def test_apply_persisted_model_router_strategy_overrides(admin_client):
    from serving.servers.routers.admin.provider_routes import (
        apply_persisted_model_router_strategy_overrides,
    )

    _client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)
    op_store.list_settings.return_value = [
        {
            "key": "model_router_strategy:on-demand-only",
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
    change = SimpleNamespace(previous_router=object(), router=object())
    registry.prepare_router_strategy_change.return_value = change
    registry.cached_routers.return_value = [change.router]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )

    await apply_persisted_model_router_strategy_overrides(services, op_store)

    registry.prepare_router_strategy_change.assert_called_once_with("on-demand-only", "fixed")
    registry.commit_router_strategy_change.assert_called_once_with(change)


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
        None,
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
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_put_provider_route_clears_openrouter_endpoint_pricing_on_retarget(
    admin_client,
):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.list_provider_route_configs_for_model.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:chutes-api",
            "provider": "openrouter[parasail]",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 8000,
            "concurrency_limit": None,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    openrouter_response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 8000,
        },
        headers=AUTH,
    )
    assert openrouter_response.status_code == 200, openrouter_response.text
    openrouter_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert openrouter_adapter.config.pricing == OPENROUTER_PARASAIL_PRICING
    assert openrouter_adapter.config.route_metadata["pricing_source"] == "openrouter_endpoint"
    assert openrouter_adapter.config.route_metadata["pricing_provider"] == "parasail/fp8"
    verify_mock.assert_awaited_once()
    fake_routewise.refresh_route_table.assert_called_once_with()

    op_store.upsert_provider_route_config.reset_mock()
    verify_mock.reset_mock()
    fake_routewise.refresh_route_table.reset_mock()
    op_store.list_provider_route_configs_for_model.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:chutes-api",
            "provider": "chutes",
            "base_url": "https://llm.chutes.ai/v1",
            "api_key_id": None,
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5-TEE",
            "quota_limit": 8000,
            "concurrency_limit": None,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    chutes_response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "chutes",
            "base_url": "https://llm.chutes.ai/v1",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5-TEE",
        },
        headers=AUTH,
    )

    assert chutes_response.status_code == 200, chutes_response.text
    op_store.upsert_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:chutes-api",
        "chutes",
        None,
        "https://llm.chutes.ai/v1",
        None,
        "MiniMaxAI/MiniMax-M2.5-TEE",
        8000,
        None,
        "127.0.0.1",
    )
    retargeted_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert retargeted_adapter.config.provider == "chutes"
    assert retargeted_adapter.config.pricing == {
        "prompt": "0",
        "completion": "0",
        "image": "0",
        "request": "0",
        "input_cache_reads": "0",
        "input_cache_writes": "0",
    }
    assert "pricing_source" not in retargeted_adapter.config.route_metadata
    assert "pricing_provider" not in retargeted_adapter.config.route_metadata
    verify_mock.assert_awaited_once()
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_put_provider_route_persists_effective_quota_when_payload_omits_limit(
    admin_client,
):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.list_provider_route_configs_for_model.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:chutes-api",
            "provider": "openrouter[parasail]",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 5000,
            "concurrency_limit": None,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["quota_limit"] == 5000
    op_store.upsert_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:chutes-api",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        5000,
        None,
        "127.0.0.1",
    )

    updated_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert updated_adapter.config.quota == {"limit": 5000}
    verify_mock.assert_awaited_once()
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_put_provider_route_updates_concurrency_limit_for_concurrency_override(
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
            "route_id": "minimax-fast:featherless-api",
            "provider": "openrouter[parasail]",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": None,
            "concurrency_limit": 3,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "concurrency_limit": 3,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "featherless"
    assert payload["upstream_provider"] == "openrouter"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["route_type"] == "concurrency"
    assert payload["quota_limit"] is None
    assert payload["concurrency_limit"] == 3
    op_store.upsert_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:featherless-api",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        None,
        3,
        "127.0.0.1",
    )

    updated_adapter = route_executor.routes["minimax-fast"].raw_adapters[1][0]
    assert updated_adapter.config.provider == "openrouter"
    assert updated_adapter.config.openrouter_pinned_provider == "parasail"
    assert updated_adapter.config.provider_type == "concurrency"
    assert updated_adapter.config.route_metadata["route_provider"] == "featherless"
    assert updated_adapter.config.route_metadata["upstream_provider"] == "openrouter[parasail]"
    assert updated_adapter.config.concurrency_pool == "featherless-minimax-fast"
    assert updated_adapter.config.concurrency == {"limit": 3}
    verify_mock.assert_awaited_once()
    fake_routewise.refresh_route_table.assert_called_once_with()


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
        None,
        "127.0.0.1",
    )

    updated_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert updated_adapter.config.provider == "openrouter"
    assert updated_adapter.config.openrouter_pinned_provider == "chutes"
    assert updated_adapter.config.route_metadata["route_provider"] == "chutes"
    assert updated_adapter.config.route_metadata["upstream_provider"] == "openrouter[chutes]"
    assert updated_adapter.config.quota == {"limit": 5000}
    verify_mock.assert_awaited_once()
    fake_routewise.refresh_route_table.assert_called_once_with()


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
    fake_routewise.refresh_route_table.assert_not_called()


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
    fake_routewise.refresh_route_table.assert_not_called()


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
    fake_routewise.refresh_route_table.assert_not_called()


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
    fake_routewise.refresh_route_table.assert_not_called()


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
    fake_routewise.refresh_route_table.assert_not_called()


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
    assert fake_routewise.refresh_route_table.call_count == 2


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
        None,
        "127.0.0.1",
    )
    verify_mock.assert_awaited_once()

    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.provider == "openrouter"
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"
    assert runtime_adapter.config.pricing == OPENROUTER_PARASAIL_PRICING
    assert runtime_adapter.config.route_metadata["pricing_source"] == "openrouter_endpoint"
    assert runtime_adapter.config.route_metadata["pricing_provider"] == "parasail/fp8"
    assert runtime_adapter.config.route_metadata["runtime_candidate"] is True
    assert runtime_adapter.config.route_metadata["route_provider"] == "openrouter[parasail]"
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_provider_route_candidate_adds_direct_minimax_route(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.list_provider_keys_full.return_value = ["minimax-db-key-1234567890"]

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "minimax",
            "base_url": "https://api.minimax.io/v1",
            "provider_model_id": "MiniMax-M2.5",
            "weight": 2.0,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "runtime"
    assert payload["route_id"] == "minimax-fast:minimax-api"
    assert payload["provider"] == "minimax"
    assert payload["upstream_provider"] == "minimax"
    assert payload["key_provider"] == "minimax"
    assert payload["base_url"] == "https://api.minimax.io/v1"
    assert payload["provider_model_id"] == "MiniMax-M2.5"
    assert payload["effective_weight"] == 2.0
    op_store.upsert_provider_route_candidate.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:minimax-api",
        "on_demand",
        "minimax",
        None,
        "https://api.minimax.io/v1",
        None,
        "MiniMax-M2.5",
        None,
        None,
        2.0,
        None,
        "127.0.0.1",
    )
    verify_mock.assert_awaited_once()

    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.provider == "minimax"
    assert runtime_adapter.config.provider_profile == "minimax"
    assert runtime_adapter.config.route_metadata["runtime_candidate"] is True
    assert runtime_adapter.config.route_metadata["route_provider"] == "minimax"
    assert runtime_adapter.config.route_metadata["upstream_provider"] == "minimax"
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_provider_route_candidate_drops_template_pricing_schedule(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.list_provider_keys_full.return_value = ["minimax-db-key-1234567890"]
    schedule = PricingSchedule.from_raw(
        {
            "effective_at": "2026-08-16T16:00:00Z",
            "timezone": "UTC",
            "default": {"prompt": "0.22", "completion": "0.66"},
            "windows": [],
        }
    )
    for entry in route_executor.routes["minimax-fast"].raw_adapters:
        entry[0].config.pricing_schedule = schedule

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "minimax",
            "base_url": "https://api.minimax.io/v1",
            "provider_model_id": "MiniMax-M2.5",
            "weight": 2.0,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    # ``upsert_provider_route_candidate`` persists ``pricing`` and nothing else,
    # so a runtime route that inherited the template's schedule would price one
    # way in memory and another way once a restart rehydrates it from the store.
    assert runtime_adapter.config.route_metadata["runtime_candidate"] is True
    assert runtime_adapter.config.pricing_schedule is None


@pytest.mark.asyncio
async def test_post_provider_route_candidate_adds_openrouter_concurrency_route(admin_client):
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
            "route_type": "concurrency",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "concurrency_limit": 2,
            "weight": 1.0,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "runtime"
    assert payload["route_id"] == "minimax-fast:openrouter[parasail]-api"
    assert payload["route_type"] == "concurrency"
    assert payload["upstream_provider"] == "openrouter"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["quota_limit"] is None
    assert payload["concurrency_limit"] == 2
    op_store.upsert_provider_route_candidate.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:openrouter[parasail]-api",
        "concurrency",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        None,
        2,
        1.0,
        None,
        "127.0.0.1",
    )
    verify_mock.assert_awaited_once()

    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.provider == "openrouter"
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"
    assert runtime_adapter.config.provider_type == "concurrency"
    assert runtime_adapter.config.concurrency_pool == (
        "minimax-fast:openrouter[parasail]-api:runtime-concurrency"
    )
    assert runtime_adapter.config.concurrency == {"limit": 2}
    assert runtime_adapter.config.route_metadata["runtime_candidate"] is True
    assert runtime_adapter.config.route_metadata["route_provider"] == "openrouter[parasail]"
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_provider_route_candidate_rejects_unpinned_openrouter_concurrency(
    admin_client,
):
    client, op_store, _route_executor, _fake_routewise, verify_mock = admin_client

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "concurrency",
            "upstream_provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "concurrency_limit": 2,
            "weight": 1.0,
        },
        headers=AUTH,
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "openrouter concurrency routes require openrouter_provider"
    )
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    verify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_provider_route_candidate_updates_openrouter_concurrency_limit(
    admin_client,
):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    create_response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "concurrency",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "concurrency_limit": 2,
            "weight": 1.0,
        },
        headers=AUTH,
    )
    assert create_response.status_code == 200, create_response.text
    op_store.upsert_provider_route_candidate.reset_mock()
    fake_routewise.refresh_route_table.reset_mock()
    verify_mock.reset_mock()

    response = await client.patch(
        "/admin/routing/provider-route-candidates/minimax-fast/"
        "minimax-fast:openrouter[parasail]-api",
        json={"concurrency_limit": 4},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["route_type"] == "concurrency"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["concurrency_limit"] == 4
    op_store.upsert_provider_route_candidate.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:openrouter[parasail]-api",
        "concurrency",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        None,
        4,
        1.0,
        None,
        "127.0.0.1",
    )
    verify_mock.assert_not_awaited()
    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.concurrency == {"limit": 4}
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_patch_provider_route_candidate_rejects_config_concurrency_limit(
    admin_client,
):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client

    response = await client.patch(
        "/admin/routing/provider-route-candidates/minimax-fast/minimax-fast:featherless-api",
        json={"concurrency_limit": 4},
        headers=AUTH,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "Only runtime OpenRouter concurrency routes can update concurrency_limit"
    )
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    featherless_adapter = route_executor.routes["minimax-fast"].raw_adapters[1][0]
    assert featherless_adapter.config.concurrency == {"limit": 1}


@pytest.mark.asyncio
async def test_post_provider_route_candidate_rejects_resource_route_for_fixed_model(admin_client):
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    adapter = _compat_adapter(
        model_id="fixed-only",
        provider="openrouter",
        endpoint_id="fixed-only:openrouter-api",
        base_url="https://openrouter.ai/api/v1",
        provider_model_id="minimax/minimax-m2.5",
    )
    route_executor.register_route("fixed-only", [(adapter, 1.0)])

    strategy_response = await client.patch(
        "/admin/routing/provider-route-strategies/fixed-only",
        json={"strategy": "fixed"},
        headers=AUTH,
    )
    assert strategy_response.status_code == 200, strategy_response.text
    op_store.upsert_provider_route_candidate.reset_mock()
    op_store.set_setting.reset_mock()

    response = await client.post(
        "/admin/routing/provider-route-candidates/fixed-only",
        json={
            "route_type": "concurrency",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "concurrency_limit": 2,
            "weight": 1.0,
        },
        headers=AUTH,
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "concurrency routes require routewise strategy"
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    verify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_resource_candidate_add_racing_fixed_patch_cannot_publish_invalid_pair(
    admin_client,
):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _register_on_demand_only_route(route_executor)
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    candidate_persist_entered = asyncio.Event()
    release_candidate_persist = asyncio.Event()
    fixed_patch_validated = asyncio.Event()

    async def block_candidate_persist(*_args, **_kwargs) -> None:
        candidate_persist_entered.set()
        await release_candidate_persist.wait()

    op_store.upsert_provider_route_candidate.side_effect = block_candidate_persist
    registry = client.app.state.services.model_router_registry

    def signal_fixed_validation(_model_id: str, strategy: str) -> None:
        if strategy == "fixed":
            fixed_patch_validated.set()

    registry.validate_router_strategy.side_effect = signal_fixed_validation

    add_candidate = asyncio.create_task(
        client.post(
            "/admin/routing/provider-route-candidates/on-demand-only",
            json={
                "route_type": "concurrency",
                "upstream_provider": "openrouter",
                "openrouter_provider": "parasail",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_id": "db-openrouter",
                "provider_model_id": "openai/gpt-oss-20b",
                "concurrency_limit": 2,
                "weight": 1,
            },
            headers=AUTH,
        )
    )
    await candidate_persist_entered.wait()
    switch_to_fixed = asyncio.create_task(
        client.patch(
            "/admin/routing/provider-route-strategies/on-demand-only",
            json={"strategy": "fixed"},
            headers=AUTH,
        )
    )
    await fixed_patch_validated.wait()

    assert not switch_to_fixed.done()
    release_candidate_persist.set()
    add_response, patch_response = await asyncio.gather(add_candidate, switch_to_fixed)

    assert add_response.status_code == 200, add_response.text
    assert patch_response.status_code == 422, patch_response.text
    assert "fixed strategy cannot be used" in patch_response.json()["detail"]
    assert registry.get_router_name("on-demand-only") == "routewise"
    route_types = {
        provider_routes._route_type(adapter)
        for adapter, _weight, _endpoint_id in provider_routes._raw_route_entries(
            route_executor.routes["on-demand-only"]
        )
    }
    assert "concurrency" in route_types
    op_store.set_setting.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_route_candidate_accepts_numbered_env_key(admin_client, monkeypatch):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    base_key = "sk-or-base111111111111111111"
    numbered_key = "sk-or-numbered222222222222"
    monkeypatch.setenv("OPENROUTER_API_KEY", base_key)
    monkeypatch.setenv("OPENROUTER_API_KEY2", numbered_key)
    monkeypatch.delenv("OPENROUTER_API_KEY3", raising=False)
    env_key_id = provider_routes._env_key_id(numbered_key)

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": env_key_id,
            "provider_model_id": "minimax/minimax-m2.5",
            "weight": 1.5,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "runtime"
    assert payload["upstream_provider"] == "openrouter"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["api_key"]["id"] == env_key_id
    assert payload["api_key"]["source"] == "env"
    assert payload["api_key"]["key_prefix"] == provider_routes._mask(numbered_key)
    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.api_keys == [numbered_key]
    op_store.upsert_provider_route_candidate.assert_awaited_once()
    verify_mock.assert_awaited_once()
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_provider_route_model_creates_runtime_model(admin_client):
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
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1.25,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["model_id"] == "deepseek-v4-flash"
    assert payload["source"] == "runtime"
    assert payload["strategy"] == "fixed"
    assert payload["route_id"] == "deepseek-v4-flash:openrouter[parasail]-api"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["api_key_id"] == "db-openrouter"
    assert payload["api_key"] == {
        "id": "db-openrouter",
        "provider": "openrouter",
        "label": "staging",
        "key_prefix": "openrou...7890",
        "source": "db",
    }
    assert payload["effective_weight"] == 1.25
    op_store.upsert_provider_route_candidate.assert_awaited_once_with(
        "deepseek-v4-flash",
        "deepseek-v4-flash:openrouter[parasail]-api",
        "on_demand",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "deepseek/deepseek-v4-flash",
        None,
        None,
        1.25,
        RUNTIME_PRICING,
        "127.0.0.1",
    )
    op_store.set_setting.assert_has_awaits(
        [
            call(
                "model_router_strategy:deepseek-v4-flash",
                "fixed",
                "string",
                "127.0.0.1",
            ),
            call(
                "model_required_role:deepseek-v4-flash",
                "admin",
                "string",
                "127.0.0.1",
            ),
        ]
    )
    verify_mock.assert_awaited_once()

    assert route_executor.routes["deepseek-v4-flash"].required_role == "admin"
    runtime_adapter = route_executor.routes["deepseek-v4-flash"].raw_adapters[0][0]
    assert runtime_adapter.config.id == "deepseek-v4-flash"
    assert runtime_adapter.config.provider == "openrouter"
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"
    assert runtime_adapter.config.pricing == RUNTIME_PRICING
    assert runtime_adapter.config.route_metadata["api_key_id"] == "db-openrouter"
    assert runtime_adapter.config.route_metadata["runtime_candidate"] is True

    list_response = await client.get(
        "/admin/routing/provider-routes/deepseek-v4-flash",
        headers=AUTH,
    )
    assert list_response.status_code == 200, list_response.text
    route = list_response.json()["routes"][0]
    assert route["api_key_id"] == "db-openrouter"
    assert route["api_key"]["source"] == "db"
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_provider_route_model_rejects_orphaned_routewise_settings(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    model_id = "deepseek-v4-flash"
    orphaned_key = model_routewise_setting_keys(model_id)[0]
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    op_store.get_setting.side_effect = lambda key: (
        {"key": key, "value": "0.4", "value_type": "float"} if key == orphaned_key else None
    )

    response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": model_id,
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1.0,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Persisted runtime model state already exists: {model_id}"
    )
    assert model_id not in route_executor.routes
    op_store.upsert_provider_route_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_provider_route_model_creates_openrouter_concurrency_model(admin_client):
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
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "routewise",
            "route_type": "concurrency",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "concurrency_limit": 2,
            "weight": 1.0,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["model_id"] == "deepseek-v4-flash"
    assert payload["source"] == "runtime"
    assert payload["strategy"] == "routewise"
    assert payload["route_id"] == "deepseek-v4-flash:openrouter[parasail]-api"
    assert payload["route_type"] == "concurrency"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["quota_limit"] is None
    assert payload["concurrency_limit"] == 2
    op_store.upsert_provider_route_candidate.assert_awaited_once_with(
        "deepseek-v4-flash",
        "deepseek-v4-flash:openrouter[parasail]-api",
        "concurrency",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "deepseek/deepseek-v4-flash",
        None,
        2,
        1.0,
        RUNTIME_PRICING,
        "127.0.0.1",
    )
    verify_mock.assert_awaited_once()

    assert route_executor.routes["deepseek-v4-flash"].required_role == "admin"
    runtime_adapter = route_executor.routes["deepseek-v4-flash"].raw_adapters[0][0]
    assert runtime_adapter.config.provider == "openrouter"
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"
    assert runtime_adapter.config.provider_type == "concurrency"
    assert runtime_adapter.config.concurrency_pool == (
        "deepseek-v4-flash:openrouter[parasail]-api:runtime-concurrency"
    )
    assert runtime_adapter.config.concurrency == {"limit": 2}
    assert runtime_adapter.config.route_metadata["runtime_candidate"] is True
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_provider_route_model_rejects_unpinned_openrouter_concurrency(
    admin_client,
):
    client, op_store, _route_executor, _fake_routewise, verify_mock = admin_client

    response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "routewise",
            "route_type": "concurrency",
            "upstream_provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "concurrency_limit": 2,
            "weight": 1.0,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "openrouter concurrency routes require openrouter_provider"
    )
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    verify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_provider_route_model_rejects_resource_route_for_fixed_strategy(admin_client):
    client, op_store, _route_executor, _fake_routewise, verify_mock = admin_client

    response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "route_type": "concurrency",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "concurrency_limit": 2,
            "weight": 1.0,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "concurrency routes require routewise strategy"
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    verify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_provider_route_candidate_persists_pricing_for_runtime_model(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
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

    create_model = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1.25,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )
    assert create_model.status_code == 200, create_model.text
    op_store.upsert_provider_route_candidate.reset_mock()

    response = await client.post(
        "/admin/routing/provider-route-candidates/deepseek-v4-flash",
        json={
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_sort": "throughput",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 0.75,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["route_id"] == "deepseek-v4-flash:openrouter-api"
    op_store.upsert_provider_route_candidate.assert_awaited_once_with(
        "deepseek-v4-flash",
        "deepseek-v4-flash:openrouter-api",
        "on_demand",
        "openrouter",
        "throughput",
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "deepseek/deepseek-v4-flash",
        None,
        None,
        0.75,
        RUNTIME_PRICING,
        "127.0.0.1",
    )
    runtime_adapter = route_executor.routes["deepseek-v4-flash"].raw_adapters[-1][0]
    assert runtime_adapter.config.pricing == RUNTIME_PRICING
    assert fake_routewise.refresh_route_table.call_count == 2


@pytest.mark.asyncio
async def test_post_provider_route_model_accepts_explicit_required_role(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")

    response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "required_role": "free",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1.25,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert route_executor.routes["deepseek-v4-flash"].required_role == "free"
    op_store.set_setting.assert_has_awaits(
        [
            call(
                "model_router_strategy:deepseek-v4-flash",
                "fixed",
                "string",
                "127.0.0.1",
            ),
            call(
                "model_required_role:deepseek-v4-flash",
                "free",
                "string",
                "127.0.0.1",
            ),
        ]
    )


@pytest.mark.asyncio
async def test_post_provider_route_model_rejects_invalid_required_role(admin_client):
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client

    response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "required_role": "world",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1.25,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 422
    assert "deepseek-v4-flash" not in route_executor.routes
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    op_store.set_setting.assert_not_awaited()
    verify_mock.assert_not_awaited()


class _TrackingLock:
    """Context manager that counts acquisitions for router-lock assertions."""

    def __init__(self) -> None:
        self.enter_count = 0

    def __enter__(self) -> _TrackingLock:
        self.enter_count += 1
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_post_provider_route_model_registers_under_router_lock(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    tracking_lock = _TrackingLock()
    route_executor._lock = tracking_lock

    response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert "deepseek-v4-flash" in route_executor.routes
    assert tracking_lock.enter_count >= 1


@pytest.mark.asyncio
async def test_post_provider_route_model_rollback_pops_route_under_router_lock(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.set_setting.side_effect = RuntimeError("settings down")
    tracking_lock = _TrackingLock()
    route_executor._lock = tracking_lock

    with pytest.raises(RuntimeError, match="settings down"):
        await client.post(
            "/admin/routing/provider-route-models",
            json={
                "model_id": "deepseek-v4-flash",
                "strategy": "fixed",
                "route_type": "on_demand",
                "upstream_provider": "openrouter",
                "openrouter_provider": "parasail",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_id": "db-openrouter",
                "provider_model_id": "deepseek/deepseek-v4-flash",
                "weight": 1,
                "pricing": RUNTIME_PRICING,
            },
            headers=AUTH,
        )

    assert "deepseek-v4-flash" not in route_executor.routes
    # register_route on install + routes.pop on rollback both acquire the lock.
    assert tracking_lock.enter_count >= 2


@pytest.mark.asyncio
async def test_post_provider_route_model_rolls_back_when_install_rebuild_fails(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    fake_routewise.refresh_route_table.side_effect = [
        RuntimeError("rebuild failed"),
        None,
        None,
    ]

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await client.post(
            "/admin/routing/provider-route-models",
            json={
                "model_id": "deepseek-v4-flash",
                "strategy": "fixed",
                "route_type": "on_demand",
                "upstream_provider": "openrouter",
                "openrouter_provider": "parasail",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_id": "db-openrouter",
                "provider_model_id": "deepseek/deepseek-v4-flash",
                "weight": 1.25,
                "pricing": RUNTIME_PRICING,
            },
            headers=AUTH,
        )

    assert "deepseek-v4-flash" not in route_executor.routes
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == 1
    op_store.upsert_provider_route_candidate.assert_awaited_once()
    op_store.delete_provider_route_candidate.assert_awaited_once_with(
        "deepseek-v4-flash",
        "deepseek-v4-flash:openrouter[parasail]-api",
    )
    assert fake_routewise.refresh_route_table.call_count == 3


@pytest.mark.asyncio
async def test_post_provider_route_model_rolls_back_when_setting_fails(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.set_setting.side_effect = RuntimeError("settings down")

    with pytest.raises(RuntimeError, match="settings down"):
        await client.post(
            "/admin/routing/provider-route-models",
            json={
                "model_id": "deepseek-v4-flash",
                "strategy": "fixed",
                "route_type": "on_demand",
                "upstream_provider": "openrouter",
                "openrouter_provider": "parasail",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_id": "db-openrouter",
                "provider_model_id": "deepseek/deepseek-v4-flash",
                "weight": 1.25,
                "pricing": RUNTIME_PRICING,
            },
            headers=AUTH,
        )

    assert "deepseek-v4-flash" not in route_executor.routes
    op_store.upsert_provider_route_candidate.assert_awaited_once()
    op_store.delete_provider_route_candidate.assert_awaited_once_with(
        "deepseek-v4-flash",
        "deepseek-v4-flash:openrouter[parasail]-api",
    )
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == 1
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_provider_route_model_rejects_existing_model(admin_client):
    client, op_store, _route_executor, _fake_routewise, verify_mock = admin_client

    response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "minimax-fast",
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "provider_model_id": "minimax/minimax-m2.5",
            "weight": 1,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 409
    assert "Model already exists" in response.json()["detail"]
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    verify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_same_id_model_create_loser_does_not_detach_winner(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    both_verified = asyncio.Event()
    release_verification = asyncio.Event()
    verified_candidates: list[object] = []

    async def verify_together(candidate) -> None:
        verified_candidates.append(candidate)
        if len(verified_candidates) == 2:
            both_verified.set()
        await release_verification.wait()

    verify_mock.side_effect = verify_together
    payload = {
        "model_id": "deepseek-v4-flash",
        "strategy": "routewise",
        "route_type": "on_demand",
        "upstream_provider": "openrouter",
        "openrouter_provider": "parasail",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_id": "db-openrouter",
        "provider_model_id": "deepseek/deepseek-v4-flash",
        "weight": 1,
        "pricing": RUNTIME_PRICING,
    }

    first = asyncio.create_task(
        client.post(
            "/admin/routing/provider-route-models",
            json=payload,
            headers=AUTH,
        )
    )
    second = asyncio.create_task(
        client.post(
            "/admin/routing/provider-route-models",
            json=payload,
            headers=AUTH,
        )
    )
    await both_verified.wait()
    release_verification.set()
    responses = await asyncio.gather(first, second)

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert "deepseek-v4-flash" in route_executor.routes
    services = client.app.state.services
    registry = services.model_router_registry
    assert registry.get_router_name("deepseek-v4-flash") == "routewise"
    assert registry.get_router("deepseek-v4-flash") is fake_routewise
    assert services.managed_routers == [fake_routewise]
    fake_routewise.stop.assert_not_awaited()
    registry.clear_router_override.assert_not_called()
    op_store.upsert_provider_route_candidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_model_stays_unpublished_until_persistence_and_strategy_commit(
    admin_client,
):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    persist_entered = asyncio.Event()
    release_persist = asyncio.Event()

    async def block_candidate_persist(*_args, **_kwargs) -> None:
        persist_entered.set()
        await release_persist.wait()

    op_store.upsert_provider_route_candidate.side_effect = block_candidate_persist
    create_model = asyncio.create_task(
        client.post(
            "/admin/routing/provider-route-models",
            json={
                "model_id": "deepseek-v4-flash",
                "strategy": "fixed",
                "route_type": "on_demand",
                "upstream_provider": "openrouter",
                "openrouter_provider": "parasail",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_id": "db-openrouter",
                "provider_model_id": "deepseek/deepseek-v4-flash",
                "weight": 1,
                "pricing": RUNTIME_PRICING,
            },
            headers=AUTH,
        )
    )
    await persist_entered.wait()

    staged_route = route_executor.routes["deepseek-v4-flash"]
    assert staged_route.published is False
    assert route_executor._select_adapter("deepseek-v4-flash") is None
    with pytest.raises(ValueError, match="No route configured"):
        await route_executor.chat_completion("deepseek-v4-flash", [])
    registry = client.app.state.services.model_router_registry
    registry.commit_router_strategy_change.assert_not_called()

    release_persist.set()
    response = await create_model

    assert response.status_code == 200, response.text
    assert staged_route.published is True
    registry.commit_router_strategy_change.assert_called_once()
    assert registry.get_router_name("deepseek-v4-flash") == "fixed"
    assert route_executor._select_adapter("deepseek-v4-flash") is staged_route.adapters[0][0]


@pytest.mark.asyncio
async def test_real_routewise_runtime_model_is_privately_staged_then_rebound_to_live_table(
    admin_client,
):
    client, op_store, _fixture_router, _fake_routewise, _verify_mock = admin_client
    services = client.app.state.services
    route_executor = RouteExecutor()
    existing_model_id = "existing-routewise"
    runtime_model_id = "deepseek-v4-flash"
    _register_on_demand_only_route(route_executor, existing_model_id)
    registry = ModelRouterRegistry(
        models_config={existing_model_id: {"router": "routewise"}},
        shared_fixed_router=route_executor,
    )
    services.router = route_executor
    services.model_router_registry = registry
    services.managed_routers.clear()

    existing_routewise = registry.get_router(existing_model_id)
    assert isinstance(existing_routewise, RouteWiseRouter)
    assert set(existing_routewise.route_candidates) == {existing_model_id}

    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    persist_entered = asyncio.Event()
    release_persist = asyncio.Event()

    async def block_candidate_persist(*_args, **_kwargs) -> None:
        persist_entered.set()
        await release_persist.wait()

    op_store.upsert_provider_route_candidate.side_effect = block_candidate_persist
    create_model = asyncio.create_task(
        client.post(
            "/admin/routing/provider-route-models",
            json={
                "model_id": runtime_model_id,
                "strategy": "routewise",
                "route_type": "on_demand",
                "upstream_provider": "openrouter",
                "openrouter_provider": "parasail",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_id": "db-openrouter",
                "provider_model_id": "deepseek/deepseek-v4-flash",
                "weight": 1,
                "pricing": RUNTIME_PRICING,
            },
            headers=AUTH,
        )
    )

    try:
        await asyncio.wait_for(persist_entered.wait(), timeout=5)
        staged_route = route_executor.routes[runtime_model_id]
        assert staged_route.published is False
        assert runtime_model_id not in {
            route.canonical_model_id for route in route_executor.iter_effective_routes()
        }
        assert runtime_model_id not in existing_routewise.route_candidates
        assert registry.get_router(runtime_model_id) is route_executor
        assert registry.get_router_override(runtime_model_id) is None
        assert services.managed_routers == []
    finally:
        release_persist.set()
        response = await asyncio.wait_for(create_model, timeout=5)

    assert response.status_code == 200, response.text
    active_routewise = registry.get_router(runtime_model_id)
    assert isinstance(active_routewise, RouteWiseRouter)
    try:
        assert staged_route.published is True
        assert active_routewise.route_table is route_executor
        assert runtime_model_id in active_routewise.route_candidates
        assert runtime_model_id in existing_routewise.route_candidates
        assert registry.get_router_override(runtime_model_id) == "routewise"
        assert services.managed_routers == [active_routewise]
    finally:
        await active_routewise.stop()
        services.managed_routers.clear()


@pytest.mark.asyncio
async def test_real_routewise_quota_only_runtime_model_rolls_back_without_leaks(
    admin_client,
):
    client, op_store, _fixture_router, _fake_routewise, _verify_mock = admin_client
    services = client.app.state.services
    route_executor = RouteExecutor()
    registry = ModelRouterRegistry(models_config={}, shared_fixed_router=route_executor)
    services.router = route_executor
    services.model_router_registry = registry
    services.managed_routers.clear()
    model_id = "deepseek-v4-flash"
    staged_routes = []

    async def record_staged_route(*_args, **_kwargs) -> None:
        staged_routes.append(route_executor.routes[model_id])

    op_store.upsert_provider_route_candidate.side_effect = record_staged_route

    response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": model_id,
            "strategy": "routewise",
            "route_type": "quota",
            "upstream_provider": "chutes",
            "base_url": "https://llm.chutes.ai/v1",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5-TEE",
            "quota_limit": 5000,
            "weight": 1,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 409, response.text
    assert "quota-only pools" in response.json()["detail"]
    assert len(staged_routes) == 1
    assert staged_routes[0].published is False
    assert model_id not in route_executor.routes
    assert registry.get_router_override(model_id) is None
    assert registry.get_router(model_id) is route_executor
    assert all(not isinstance(router, RouteWiseRouter) for router in registry.cached_routers())
    assert services.managed_routers == []

    route_id = op_store.upsert_provider_route_candidate.await_args.args[1]
    op_store.delete_provider_route_candidate.assert_awaited_once_with(model_id, route_id)
    assert {call.args[0] for call in op_store.delete_setting.await_args_list} == {
        f"model_router_strategy:{model_id}",
        f"model_required_role:{model_id}",
    }


@pytest.mark.asyncio
async def test_verify_provider_route_model_does_not_create_model(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")

    response = await client.post(
        "/admin/routing/provider-route-model-verifications",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    verify_mock.assert_awaited_once()
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    assert "deepseek-v4-flash" not in route_executor.routes
    fake_routewise.refresh_route_table.assert_not_called()


@pytest.mark.asyncio
async def test_provider_route_list_resolves_numbered_env_key_ref(admin_client, monkeypatch):
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client
    base_key = "rc_base111111111111111111111111"
    numbered_key = "rc_numbered222222222222222222"
    monkeypatch.setenv("FEATHERLESS_API_KEY", base_key)
    monkeypatch.setenv("FEATHERLESS_API_KEY2", numbered_key)
    monkeypatch.delenv("FEATHERLESS_API_KEY3", raising=False)
    env_key_id = provider_routes._env_key_id(numbered_key)
    op_store.list_provider_route_configs_for_model.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:chutes-api",
            "provider": "featherless",
            "openrouter_sort": None,
            "base_url": "https://api.featherless.ai/v1",
            "api_key_id": env_key_id,
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5",
            "quota_limit": None,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    response = await client.get("/admin/routing/provider-routes/minimax-fast", headers=AUTH)

    assert response.status_code == 200, response.text
    route = next(
        row for row in response.json()["routes"] if row["route_id"] == "minimax-fast:chutes-api"
    )
    assert route["upstream_provider"] == "featherless"
    assert route["api_key"]["id"] == env_key_id
    assert route["api_key"]["source"] == "env"
    assert route["api_key"]["key_prefix"] == provider_routes._mask(numbered_key)


@pytest.mark.asyncio
async def test_runtime_candidate_list_ignores_stale_override_row(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
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

    create = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "weight": 1.0,
        },
        headers=AUTH,
    )
    assert create.status_code == 200, create.text
    route_id = "minimax-fast:openrouter[parasail]-api"
    op_store.list_provider_route_configs_for_model.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": route_id,
            "provider": "chutes",
            "openrouter_sort": None,
            "base_url": "https://llm.chutes.ai/v1",
            "api_key_id": None,
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5-TEE",
            "quota_limit": 5000,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]

    response = await client.get("/admin/routing/provider-routes/minimax-fast", headers=AUTH)

    assert response.status_code == 200, response.text
    runtime_row = next(row for row in response.json()["routes"] if row["route_id"] == route_id)
    assert runtime_row["source"] == "runtime"
    assert runtime_row["upstream_provider"] == "openrouter"
    assert runtime_row["openrouter_provider"] == "parasail"
    assert runtime_row["base_url"] == "https://openrouter.ai/api/v1"
    assert runtime_row["provider_model_id"] == "minimax/minimax-m2.5"
    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"


@pytest.mark.asyncio
async def test_custom_provider_route_candidate_preserves_provider_identity(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    provider_registry.register_provider_definition(
        RuntimeProviderDefinition(
            provider="acme",
            display_name="Acme",
            adapter_kind="openai_compat",
            default_base_url="https://api.acme.test/plan/v3",
        )
    )
    op_store.get_provider_key_full.return_value = ("acme", "acme-db-key-1234567890")
    op_store.list_provider_keys.return_value = [
        ProviderKeyRow(
            id="db-acme",
            provider="acme",
            key_prefix="acme-db-...7890",
            label="acme",
            status="active",
            created_at=NOW,
        )
    ]

    try:
        response = await client.post(
            "/admin/routing/provider-route-candidates/minimax-fast",
            json={
                "route_type": "on_demand",
                "upstream_provider": "acme",
                "base_url": "https://api.acme.test/plan/v3",
                "api_key_id": "db-acme",
                "provider_model_id": "acme/model",
                "weight": 1.0,
            },
            headers=AUTH,
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["provider"] == "acme"
        assert payload["upstream_provider"] == "acme"
        assert payload["key_provider"] == "acme"
        assert payload["api_key"]["provider"] == "acme"
        runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
        assert runtime_adapter.config.provider == "acme"
        assert runtime_adapter.config.chat_path == "/chat/completions"
        assert runtime_adapter._build_url() == "https://api.acme.test/plan/v3/chat/completions"
    finally:
        provider_registry.unregister_provider_definition("acme")


@pytest.mark.asyncio
async def test_custom_provider_route_update_uses_definition_chat_path(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    provider_registry.register_provider_definition(
        RuntimeProviderDefinition(
            provider="tencent_token_plan",
            display_name="Tencent Token Plan",
            adapter_kind="openai_compat",
            default_base_url="https://api.lkeap.cloud.tencent.com/plan/v3",
        )
    )
    op_store.get_provider_key_full.return_value = (
        "tencent_token_plan",
        "tencent-db-key-1234567890",
    )

    try:
        response = await client.post(
            "/admin/routing/provider-route-verifications/minimax-fast/minimax-fast:chutes-api",
            json={
                "upstream_provider": "tencent_token_plan",
                "base_url": "https://api.lkeap.cloud.tencent.com/plan/v3",
                "api_key_id": "db-tencent",
                "provider_model_id": "minimax-m2.5",
                "quota_limit": 5000,
            },
            headers=AUTH,
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}
        verify_mock.assert_awaited_once()
        update = verify_mock.await_args.args[0]
        assert update.adapter.config.provider == "tencent_token_plan"
        assert update.adapter.config.chat_path == "/chat/completions"
        assert (
            update.adapter._build_url()
            == "https://api.lkeap.cloud.tencent.com/plan/v3/chat/completions"
        )
        op_store.upsert_provider_route_config.assert_not_awaited()

        current_adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
        assert current_adapter.config.provider == "chutes"
        fake_routewise.refresh_route_table.assert_not_called()
    finally:
        provider_registry.unregister_provider_definition("tencent_token_plan")


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
    fake_routewise.refresh_route_table.assert_not_called()


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
    assert fake_routewise.refresh_route_table.call_count == 1


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
        None,
        "127.0.0.1",
    )
    verify_mock.assert_awaited_once()

    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.provider == "openrouter"
    assert runtime_adapter.config.openrouter_sort == "throughput"
    assert runtime_adapter.config.openrouter_pinned_provider is None
    assert runtime_adapter.config.route_metadata["openrouter_sort"] == "throughput"
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_provider_route_candidate_prices_pinned_openrouter_with_sort(
    admin_client,
):
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
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
    assert payload["route_id"] == "minimax-fast:openrouter[parasail]-api"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["openrouter_sort"] == "throughput"
    op_store.upsert_provider_route_candidate.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:openrouter[parasail]-api",
        "on_demand",
        "openrouter[parasail]",
        "throughput",
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        None,
        None,
        1.0,
        None,
        "127.0.0.1",
    )
    verify_mock.assert_awaited_once()

    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"
    assert runtime_adapter.config.openrouter_sort == "throughput"
    assert runtime_adapter.config.pricing == OPENROUTER_PARASAIL_PRICING
    assert runtime_adapter.config.route_metadata["pricing_provider"] == "parasail/fp8"


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
    fake_routewise.refresh_route_table.assert_not_called()


@pytest.mark.asyncio
async def test_post_provider_route_candidate_rejects_provider_base_url_mismatch(admin_client):
    client, op_store, _route_executor, fake_routewise, verify_mock = admin_client

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "quota",
            "upstream_provider": "chutes",
            "base_url": "https://api.minimax.io/v1",
            "provider_model_id": "MiniMax-M2.5",
            "quota_limit": 5000,
            "weight": 1,
        },
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "base_url host belongs to provider 'minimax', not 'chutes'"
    )
    verify_mock.assert_not_awaited()
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    fake_routewise.refresh_route_table.assert_not_called()


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
    op_store.delete_weight_override.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:openrouter[parasail]-api",
    )
    assert len(route_executor.routes["minimax-fast"].raw_adapters) == 3
    assert fake_routewise.refresh_route_table.call_count == 2


@pytest.mark.asyncio
async def test_delete_last_runtime_route_removes_whole_model(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    visibility_resolver = MagicMock()
    client.app.state.services.model_visibility_resolver = visibility_resolver
    concurrency_resolver = MagicMock()
    client.app.state.services.model_concurrency_resolver = concurrency_resolver
    weight_resolver = MagicMock()
    client.app.state.services.weight_override_resolver = weight_resolver
    routewise_settings_resolver = MagicMock()
    client.app.state.services.routewise_settings_resolver = routewise_settings_resolver
    initial_openrouter_pool_count = len(dynamic_keys.get_pools_for_provider("openrouter"))
    op_store.list_weight_overrides_for_model.return_value = [
        {
            "model_id": "deepseek-v4-flash",
            "endpoint_id": "deepseek-v4-flash:openrouter[parasail]-api",
            "weight": 0.5,
        }
    ]

    create_response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )
    assert create_response.status_code == 200, create_response.text
    assert "deepseek-v4-flash" in route_executor.routes
    fake_routewise.refresh_route_table.side_effect = RuntimeError("rebuild failed")

    response = await client.delete(
        "/admin/routing/provider-route-candidates/deepseek-v4-flash/"
        "deepseek-v4-flash:openrouter[parasail]-api",
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.json()["routes"] == []
    # The whole runtime model is gone, not left serving in memory.
    assert "deepseek-v4-flash" not in route_executor.routes
    # Every candidate/config/policy row for the model is deleted atomically so
    # hidden boot-skipped candidates cannot reappear after recreate/restart.
    op_store.delete_runtime_model_state.assert_awaited_once_with(
        "deepseek-v4-flash",
        (
            "model_required_role:deepseek-v4-flash",
            "model_router_strategy:deepseek-v4-flash",
            *model_routewise_setting_keys("deepseek-v4-flash"),
        ),
    )
    # Its visibility override and resolver cache are also cleared so recreating
    # the same model cannot inherit stale access policy.
    op_store.delete_model_visibility_override.assert_not_awaited()
    visibility_resolver.invalidate_model.assert_called_once_with("deepseek-v4-flash")
    op_store.delete_model_concurrency_exemption.assert_not_awaited()
    concurrency_resolver.invalidate_model.assert_called_once_with("deepseek-v4-flash")
    # Its route weight overrides are cleared for the same reason.
    op_store.list_weight_overrides_for_model.assert_not_awaited()
    op_store.delete_weight_override.assert_not_awaited()
    weight_resolver.clear_model.assert_called_once_with("deepseek-v4-flash")
    routewise_settings_resolver.clear_model.assert_called_once_with("deepseek-v4-flash")
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == (initial_openrouter_pool_count)


@pytest.mark.asyncio
async def test_ambiguous_last_route_delete_tombstones_live_model(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    create_response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )
    assert create_response.status_code == 200, create_response.text
    op_store.delete_runtime_model_state.side_effect = RuntimeError(
        "connection dropped after commit"
    )

    with pytest.raises(RuntimeError, match="connection dropped after commit"):
        await client.delete(
            "/admin/routing/provider-route-candidates/deepseek-v4-flash/"
            "deepseek-v4-flash:openrouter[parasail]-api",
            headers=AUTH,
        )

    assert "deepseek-v4-flash" not in route_executor.routes
    op_store.delete_provider_route_candidate_with_config.assert_awaited_once_with(
        "deepseek-v4-flash",
        "deepseek-v4-flash:openrouter[parasail]-api",
    )
    client.app.state.services.model_router_registry.clear_router_override.assert_called_once_with(
        "deepseek-v4-flash"
    )
    assert client.app.state.services.managed_routers == []


@pytest.mark.asyncio
async def test_last_route_delete_racing_candidate_add_leaves_no_orphan(admin_client):
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    create_response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )
    assert create_response.status_code == 200, create_response.text
    op_store.upsert_provider_route_candidate.reset_mock()
    registry = client.app.state.services.model_router_registry
    registry.clear_router_override.reset_mock()
    verify_mock.reset_mock()
    add_verified = asyncio.Event()
    delete_persist_entered = asyncio.Event()
    release_delete_persist = asyncio.Event()

    async def signal_add_verified(_candidate) -> None:
        add_verified.set()

    verify_mock.side_effect = signal_add_verified

    async def block_delete(*_args, **_kwargs) -> bool:
        delete_persist_entered.set()
        await release_delete_persist.wait()
        return True

    op_store.delete_runtime_model_state.side_effect = block_delete
    delete_model = asyncio.create_task(
        client.delete(
            "/admin/routing/provider-route-candidates/deepseek-v4-flash/"
            "deepseek-v4-flash:openrouter[parasail]-api",
            headers=AUTH,
        )
    )
    await delete_persist_entered.wait()
    add_candidate = asyncio.create_task(
        client.post(
            "/admin/routing/provider-route-candidates/deepseek-v4-flash",
            json={
                "route_type": "on_demand",
                "upstream_provider": "openrouter",
                "openrouter_provider": "deepinfra",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_id": "db-openrouter",
                "provider_model_id": "deepseek/deepseek-v4-flash",
                "weight": 1,
            },
            headers=AUTH,
        )
    )
    await add_verified.wait()

    assert not add_candidate.done()
    release_delete_persist.set()
    delete_response, add_response = await asyncio.gather(delete_model, add_candidate)

    assert delete_response.status_code == 200, delete_response.text
    assert add_response.status_code == 404, add_response.text
    assert "deepseek-v4-flash" not in route_executor.routes
    registry.clear_router_override.assert_called_once_with("deepseek-v4-flash")
    assert client.app.state.services.managed_routers == []
    op_store.upsert_provider_route_candidate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload", "write_method"),
    [
        (
            "PATCH",
            "/admin/models/deepseek-v4-flash/visibility",
            {"required_role": "free"},
            "set_model_visibility_override",
        ),
        (
            "PATCH",
            "/admin/models/deepseek-v4-flash/concurrency",
            {"exempt": True},
            "set_model_concurrency_exemption",
        ),
        (
            "PUT",
            "/admin/routing/weights/deepseek-v4-flash/deepseek-v4-flash:openrouter[parasail]-api",
            {"weight": 2},
            "upsert_weight_override",
        ),
    ],
)
async def test_last_route_delete_serializes_policy_writes(
    admin_client,
    method,
    path,
    payload,
    write_method,
):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    create_response = await client.post(
        "/admin/routing/provider-route-models",
        json={
            "model_id": "deepseek-v4-flash",
            "strategy": "fixed",
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "weight": 1,
            "pricing": RUNTIME_PRICING,
        },
        headers=AUTH,
    )
    assert create_response.status_code == 200, create_response.text

    delete_started = asyncio.Event()
    release_delete = asyncio.Event()

    async def block_delete(*_args, **_kwargs) -> bool:
        delete_started.set()
        await release_delete.wait()
        return True

    op_store.delete_runtime_model_state.side_effect = block_delete
    delete_task = asyncio.create_task(
        client.delete(
            "/admin/routing/provider-route-candidates/deepseek-v4-flash/"
            "deepseek-v4-flash:openrouter[parasail]-api",
            headers=AUTH,
        )
    )
    await delete_started.wait()
    policy_task = asyncio.create_task(client.request(method, path, json=payload, headers=AUTH))
    await asyncio.sleep(0)

    assert not policy_task.done()
    getattr(op_store, write_method).assert_not_awaited()
    release_delete.set()
    delete_response, policy_response = await asyncio.gather(delete_task, policy_task)

    assert delete_response.status_code == 200, delete_response.text
    assert policy_response.status_code == 404, policy_response.text
    assert "deepseek-v4-flash" not in route_executor.routes
    getattr(op_store, write_method).assert_not_awaited()


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
    fake_routewise.refresh_route_table.assert_not_called()


@pytest.mark.asyncio
async def test_apply_persisted_provider_route_candidates(admin_client):
    _client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
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
    registry.refresh_route_tables.assert_called_once_with()


@pytest.mark.asyncio
async def test_apply_persisted_provider_route_candidates_skips_mismatched_route_id(
    admin_client,
):
    _client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.list_all_provider_route_candidates.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:minimax-api",
            "route_type": "on_demand",
            "provider": "chutes",
            "openrouter_sort": None,
            "base_url": "https://api.minimax.io/v1",
            "api_key_id": None,
            "provider_model_id": "MiniMax-M2.5",
            "quota_limit": None,
            "concurrency_limit": None,
            "weight": 1.0,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]
    registry = MagicMock()
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )

    await apply_persisted_provider_route_candidates(services, op_store)

    route_ids = [
        provider_routes._route_id_for_entry(adapter, endpoint_id)
        for adapter, _weight, endpoint_id in route_executor.routes["minimax-fast"].raw_adapters
    ]
    assert "minimax-fast:minimax-api" not in route_ids
    assert len(route_ids) == 3
    op_store.list_provider_keys_full.assert_not_awaited()
    registry.refresh_route_tables.assert_not_called()


@pytest.mark.asyncio
async def test_apply_persisted_provider_route_candidates_restores_runtime_model(admin_client):
    _client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.list_all_provider_route_candidates.return_value = [
        {
            "model_id": "deepseek-v4-flash",
            "route_id": "deepseek-v4-flash:openrouter[parasail]-api",
            "route_type": "on_demand",
            "provider": "openrouter[parasail]",
            "openrouter_sort": None,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "quota_limit": None,
            "concurrency_limit": None,
            "weight": 1.25,
            "pricing": RUNTIME_PRICING,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]
    op_store.list_settings.return_value = [
        {
            "key": "model_required_role:deepseek-v4-flash",
            "value": "internal",
        },
        {
            "key": "model_router_strategy:deepseek-v4-flash",
            "value": "fixed",
        },
    ]
    registry = MagicMock()
    strategy_state = {"deepseek-v4-flash": "routewise"}
    registry.get_router_name.side_effect = lambda model_id: strategy_state.get(
        model_id,
        "routewise",
    )
    registry.validate_router_strategy = MagicMock()
    registry.get_router = MagicMock(return_value=fake_routewise)
    change = SimpleNamespace(previous_router=fake_routewise, router=route_executor)
    registry.prepare_router_strategy_change.return_value = change
    registry.commit_router_strategy_change.side_effect = lambda _change: strategy_state.__setitem__(
        "deepseek-v4-flash", "fixed"
    )
    registry.cached_routers.return_value = [route_executor]
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[],
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )

    await apply_persisted_provider_route_candidates(services, op_store)

    runtime_adapter, raw_weight, endpoint_id = route_executor.routes[
        "deepseek-v4-flash"
    ].raw_adapters[0]
    assert endpoint_id == "deepseek-v4-flash:openrouter[parasail]-api"
    assert raw_weight == 1.25
    assert route_executor.routes["deepseek-v4-flash"].required_role == "internal"
    assert runtime_adapter.config.id == "deepseek-v4-flash"
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"
    assert runtime_adapter.config.pricing == RUNTIME_PRICING
    assert runtime_adapter.config.route_metadata["runtime_candidate"] is True
    registry.prepare_router_strategy_change.assert_called_once_with("deepseek-v4-flash", "fixed")
    registry.commit_router_strategy_change.assert_called_once_with(change)
    registry.refresh_route_tables.assert_called_once_with()


@pytest.mark.asyncio
async def test_apply_persisted_provider_route_candidates_prefers_priced_runtime_seed(
    admin_client,
):
    _client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.list_all_provider_route_candidates.return_value = [
        {
            "model_id": "deepseek-v4-flash",
            "route_id": "deepseek-v4-flash:a-openrouter-api",
            "route_type": "on_demand",
            "provider": "openrouter",
            "openrouter_sort": "throughput",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "quota_limit": None,
            "concurrency_limit": None,
            "weight": 0.75,
            "pricing": None,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        },
        {
            "model_id": "deepseek-v4-flash",
            "route_id": "deepseek-v4-flash:z-openrouter[parasail]-api",
            "route_type": "on_demand",
            "provider": "openrouter[parasail]",
            "openrouter_sort": None,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "quota_limit": None,
            "concurrency_limit": None,
            "weight": 1.25,
            "pricing": RUNTIME_PRICING,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        },
    ]
    op_store.list_settings.return_value = [
        {
            "key": "model_required_role:deepseek-v4-flash",
            "value": "internal",
        },
        {
            "key": "model_router_strategy:deepseek-v4-flash",
            "value": "fixed",
        },
    ]
    registry = MagicMock()
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        managed_routers=[],
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )

    restored_routewise = await apply_persisted_provider_route_candidates(services, op_store)

    restored_entries = route_executor.routes["deepseek-v4-flash"].raw_adapters
    assert [endpoint_id for _adapter, _weight, endpoint_id in restored_entries] == [
        "deepseek-v4-flash:z-openrouter[parasail]-api",
        "deepseek-v4-flash:a-openrouter-api",
    ]
    assert all(
        adapter.config.pricing == RUNTIME_PRICING for adapter, _weight, _id in restored_entries
    )
    assert route_executor.routes["deepseek-v4-flash"].required_role == "internal"
    assert restored_routewise == set()
    assert registry.refresh_route_tables.call_count == 2


@pytest.mark.asyncio
async def test_apply_persisted_provider_route_candidates_skips_orphan_without_marker(admin_client):
    _client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    # Candidate row for a model that is NOT in the router and has no final role
    # commit marker. Even a strategy row cannot distinguish an interrupted
    # runtime create from a legitimate candidate on a removed YAML model.
    op_store.list_all_provider_route_candidates.return_value = [
        {
            "model_id": "retired-yaml-model",
            "route_id": "retired-yaml-model:openrouter[parasail]-api",
            "route_type": "on_demand",
            "provider": "openrouter[parasail]",
            "openrouter_sort": None,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "quota_limit": None,
            "concurrency_limit": None,
            "weight": 1.0,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]
    op_store.list_settings.return_value = [
        {
            "key": "model_router_strategy:retired-yaml-model",
            "value": "routewise",
        }
    ]
    services = AppServices(
        router=route_executor,
        model_router_registry=MagicMock(),
        managed_routers=[],
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )

    await apply_persisted_provider_route_candidates(services, op_store)

    # The retired model must not be resurrected as a runtime model, and the row is
    # skipped before any candidate preparation (no key material is resolved).
    assert "retired-yaml-model" not in route_executor.routes
    op_store.get_provider_key_full.assert_not_awaited()
    op_store.delete_runtime_model_state.assert_not_awaited()


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
    fake_routewise.refresh_route_table.assert_not_called()
    op_store.delete_provider_route_config.assert_awaited_once_with("minimax-fast", "route-0")


@pytest.mark.asyncio
async def test_provider_route_update_refresh_failure_restores_live_route_without_key_leak(
    admin_client,
):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    route = route_executor.routes["minimax-fast"]
    old_entries = list(route.raw_adapters)
    old_adapter = old_entries[0][0]
    openrouter_pool_count = len(dynamic_keys.get_pools_for_provider("openrouter"))
    fake_routewise.refresh_route_table.side_effect = [
        RuntimeError("rebuild failed"),
        None,
    ]

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await client.put(
            "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
            json={
                "upstream_provider": "openrouter",
                "openrouter_provider": "parasail",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_id": "db-openrouter",
                "provider_model_id": "minimax/minimax-m2.5",
                "quota_limit": 8000,
            },
            headers=AUTH,
        )

    assert route.raw_adapters == old_entries
    assert route.raw_adapters[0][0] is old_adapter
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == openrouter_pool_count
    assert fake_routewise.refresh_route_table.call_count == 2
    op_store.upsert_provider_route_config.assert_awaited_once()
    op_store.delete_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:chutes-api",
    )


@pytest.mark.asyncio
async def test_provider_route_restore_refresh_failure_keeps_override_without_key_leak(
    admin_client,
):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    update_response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "concurrency_limit": 2,
        },
        headers=AUTH,
    )
    assert update_response.status_code == 200, update_response.text
    route = route_executor.routes["minimax-fast"]
    override_entries = list(route.raw_adapters)
    override_adapter = override_entries[1][0]
    openrouter_pool_count = len(dynamic_keys.get_pools_for_provider("openrouter"))
    op_store.list_provider_route_configs_for_model.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": "minimax-fast:featherless-api",
            "provider": "openrouter[parasail]",
            "openrouter_sort": None,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": None,
            "concurrency_limit": 2,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]
    fake_routewise.refresh_route_table.reset_mock()
    fake_routewise.refresh_route_table.side_effect = [
        RuntimeError("rebuild failed"),
        None,
    ]
    op_store.upsert_provider_route_config.reset_mock()

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await client.delete(
            "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
            headers=AUTH,
        )

    assert route.raw_adapters == override_entries
    assert route.raw_adapters[1][0] is override_adapter
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == openrouter_pool_count
    assert fake_routewise.refresh_route_table.call_count == 2
    op_store.delete_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:featherless-api",
    )
    op_store.upsert_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:featherless-api",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        None,
        2,
        "127.0.0.1",
    )


@pytest.mark.asyncio
async def test_candidate_add_refresh_failure_restores_live_route_without_key_leak(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    route = route_executor.routes["minimax-fast"]
    old_entries = list(route.raw_adapters)
    openrouter_pool_count = len(dynamic_keys.get_pools_for_provider("openrouter"))
    fake_routewise.refresh_route_table.side_effect = [
        RuntimeError("rebuild failed"),
        None,
    ]

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await client.post(
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

    assert route.raw_adapters == old_entries
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == openrouter_pool_count
    assert fake_routewise.refresh_route_table.call_count == 2
    op_store.upsert_provider_route_candidate.assert_awaited_once()
    op_store.delete_provider_route_candidate.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:openrouter[parasail]-api",
    )


@pytest.mark.asyncio
async def test_candidate_delete_refresh_failure_keeps_live_route_without_key_leak(
    admin_client,
):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
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
    assert create_response.status_code == 200, create_response.text
    route = route_executor.routes["minimax-fast"]
    candidate_entries = list(route.raw_adapters)
    candidate_adapter = candidate_entries[-1][0]
    openrouter_pool_count = len(dynamic_keys.get_pools_for_provider("openrouter"))
    candidate_snapshot = {
        "model_id": "minimax-fast",
        "route_id": "minimax-fast:openrouter[parasail]-api",
        "route_type": "on_demand",
        "provider": "openrouter[parasail]",
        "openrouter_sort": None,
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_id": "db-openrouter",
        "provider_model_id": "minimax/minimax-m2.5",
        "quota_limit": None,
        "concurrency_limit": None,
        "weight": 1.0,
        "pricing": None,
        "updated_at": NOW,
        "updated_by": "seed-admin",
    }
    override_snapshot = {
        "model_id": "minimax-fast",
        "route_id": "minimax-fast:openrouter[parasail]-api",
        "provider": "openrouter[parasail]",
        "openrouter_sort": None,
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_id": "db-openrouter",
        "provider_model_id": "minimax/minimax-m2.5",
        "quota_limit": None,
        "concurrency_limit": None,
        "updated_at": NOW,
        "updated_by": "seed-admin",
    }
    op_store.list_provider_route_candidates_for_model.return_value = [candidate_snapshot]
    op_store.list_provider_route_configs_for_model.return_value = [override_snapshot]
    op_store.upsert_provider_route_candidate.reset_mock()
    op_store.upsert_provider_route_config.reset_mock()
    fake_routewise.refresh_route_table.reset_mock()
    fake_routewise.refresh_route_table.side_effect = [
        RuntimeError("rebuild failed"),
        None,
    ]

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await client.delete(
            "/admin/routing/provider-route-candidates/minimax-fast/"
            "minimax-fast:openrouter[parasail]-api",
            headers=AUTH,
        )

    assert route.raw_adapters == candidate_entries
    assert route.raw_adapters[-1][0] is candidate_adapter
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == openrouter_pool_count
    assert fake_routewise.refresh_route_table.call_count == 2
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
        1.0,
        None,
        "seed-admin",
    )
    op_store.upsert_provider_route_config.assert_awaited_once_with(
        "minimax-fast",
        "minimax-fast:openrouter[parasail]-api",
        "openrouter[parasail]",
        None,
        "https://openrouter.ai/api/v1",
        "db-openrouter",
        "minimax/minimax-m2.5",
        None,
        None,
        "seed-admin",
    )


@pytest.mark.asyncio
async def test_candidate_create_cancellation_commits_and_audits_exactly_once(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    persist_entered = asyncio.Event()
    release_persist = asyncio.Event()
    audit = provider_routes.log_admin_action

    async def block_candidate_persist(*_args, **_kwargs) -> None:
        persist_entered.set()
        await release_persist.wait()

    op_store.upsert_provider_route_candidate.side_effect = block_candidate_persist
    request = asyncio.create_task(
        client.post(
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
    )
    await persist_entered.wait()
    request.cancel()
    await asyncio.sleep(0)

    assert not request.done()
    assert len(route_executor.routes["minimax-fast"].raw_adapters) == 3
    audit.assert_not_awaited()

    release_persist.set()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert len(route_executor.routes["minimax-fast"].raw_adapters) == 4
    op_store.upsert_provider_route_candidate.assert_awaited_once()
    audit.assert_awaited_once()
    assert audit.await_args.args[2] == "routing.provider_routes.candidate.create"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["prepare", "refresh", "commit"])
async def test_persisted_runtime_model_strategy_failure_never_publishes_fixed_route(
    admin_client,
    failure_stage,
):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    services = client.app.state.services
    registry = services.model_router_registry
    model_id = "deepseek-v4-flash"
    op_store.get_provider_key_full.return_value = (
        "openrouter",
        "openrouter-db-key-1234567890",
    )
    op_store.list_all_provider_route_candidates.return_value = [
        {
            "model_id": model_id,
            "route_id": f"{model_id}:openrouter[parasail]-api",
            "route_type": "on_demand",
            "provider": "openrouter[parasail]",
            "openrouter_sort": None,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "quota_limit": None,
            "concurrency_limit": None,
            "weight": 1.0,
            "pricing": RUNTIME_PRICING,
            "updated_at": NOW,
            "updated_by": "127.0.0.1",
        }
    ]
    op_store.list_settings.return_value = [
        {
            "key": f"model_required_role:{model_id}",
            "value": "internal",
        },
        {
            "key": f"model_router_strategy:{model_id}",
            "value": "routewise",
        },
    ]
    observed_published: list[bool] = []

    def observe_unpublished() -> None:
        observed_published.append(route_executor.routes[model_id].published)

    if failure_stage == "prepare":

        def fail_prepare(*_args, **_kwargs):
            observe_unpublished()
            raise RuntimeError("prepare failed")

        registry.prepare_router_strategy_change.side_effect = fail_prepare
    elif failure_stage == "refresh":

        def fail_private_snapshot_refresh(_route_table):
            observe_unpublished()
            raise RuntimeError("refresh failed")

        fake_routewise.attach_route_table.side_effect = fail_private_snapshot_refresh
    else:

        def fail_commit(_change):
            observe_unpublished()
            raise RuntimeError("commit failed")

        registry.commit_router_strategy_change.side_effect = fail_commit

    openrouter_pool_count = len(dynamic_keys.get_pools_for_provider("openrouter"))

    restored = await apply_persisted_provider_route_candidates(services, op_store)

    assert restored == set()
    assert observed_published == [False]
    assert model_id not in route_executor.routes
    assert route_executor._select_adapter(model_id) is None
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == openrouter_pool_count
    registry.clear_router_override.assert_called_once_with(model_id)
    assert services.managed_routers == []


# ---------------------------------------------------------------------------
# Analytics-label preservation across route overrides
#
# `_prepare_route_update` sets cfg["provider"] to the upstream the target
# implies. For a route relabelled with `provider:` in models.yaml that would
# merge it back into the upstream's dashboard cohort and leave its own disable
# switch inert — and persisted overrides replay through the same path at every
# boot, so the label would not survive a restart either.
# ---------------------------------------------------------------------------


def _relabelled_route_adapter(*, label: str = "local-a", upstream: str = "vllm"):
    return _compat_adapter(
        model_id="qwen-local",
        provider=label,
        endpoint_id="qwen-local:local-8002",
        base_url="http://localhost:8002/v1",
        provider_model_id="Qwen/Qwen3.6-35B-A3B-FP8",
    ), upstream


def test_preserve_route_semantics_keeps_a_models_yaml_provider_label():
    adapter, upstream = _relabelled_route_adapter()
    adapter.config.route_metadata = {
        "provider_type": "on_demand",
        "key_provider": upstream,
        "route_provider": upstream,
        "upstream_provider": upstream,
    }
    # What _prepare_route_update would have written before preservation runs.
    cfg = {"provider": upstream, "route_metadata": dict(adapter.config.route_metadata)}

    provider_routes._preserve_route_semantics(
        cfg,
        current_adapter=adapter,
        upstream_provider=upstream,
        route_id="qwen-local:local-8002",
    )

    assert cfg["provider"] == "local-a"
    assert cfg["route_metadata"]["key_provider"] == "vllm"
    assert cfg["route_metadata"]["upstream_provider"] == "vllm"


def test_preserve_route_semantics_repins_keys_when_a_labelled_route_is_retargeted():
    adapter, upstream = _relabelled_route_adapter()
    adapter.config.route_metadata = {
        "provider_type": "on_demand",
        "key_provider": upstream,
        "route_provider": upstream,
        "upstream_provider": upstream,
    }
    cfg = {"provider": "chutes", "route_metadata": dict(adapter.config.route_metadata)}

    provider_routes._preserve_route_semantics(
        cfg,
        current_adapter=adapter,
        upstream_provider="chutes",
        route_id="qwen-local:local-8002",
    )

    # The operator's name for this route slot survives; its keys follow the new
    # upstream.
    assert cfg["provider"] == "local-a"
    assert cfg["route_metadata"]["key_provider"] == "chutes"


def test_preserve_route_semantics_leaves_an_ordinary_retarget_alone():
    adapter = _compat_adapter(
        provider="chutes",
        endpoint_id="minimax-fast:chutes-api",
        base_url="https://llm.chutes.ai/v1",
        provider_model_id="MiniMaxAI/MiniMax-M2.5-TEE",
    )
    cfg = {"provider": "openrouter", "route_metadata": dict(adapter.config.route_metadata)}

    provider_routes._preserve_route_semantics(
        cfg,
        current_adapter=adapter,
        upstream_provider="openrouter",
        route_id="minimax-fast:chutes-api",
    )

    assert cfg["provider"] == "openrouter"


def test_openrouter_pin_is_not_mistaken_for_an_analytics_label():
    """A pinned route stores `openrouter[parasail]` against an `openrouter`
    provider. Reading that divergence as a label would freeze the provider on
    every later retarget."""
    adapter = _openrouter_deepinfra_adapter()
    adapter.config.route_metadata = {
        **(adapter.config.route_metadata or {}),
        "upstream_provider": "openrouter[parasail]",
    }
    assert provider_routes._relabelled_analytics_label(adapter) is None

    cfg = {"provider": "chutes", "route_metadata": dict(adapter.config.route_metadata)}
    provider_routes._preserve_route_semantics(
        cfg,
        current_adapter=adapter,
        upstream_provider="chutes",
        route_id="minimax-fast:deepinfra-api",
    )
    assert cfg["provider"] == "chutes"
