"""Tests for admin provider route override endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.model_router_registry import ModelRouterRegistry, StaleRouterStrategyChangeError
from routing.routers import FixedRouter, adapter_supports_modalities
from routing.routewise.envelope import EnvelopeNotCalibratedError
from routing.routewise.router import RouteWiseRouter
from serving.adapters import ModelConfig, OpenAICompatAdapter, dynamic_keys, provider_registry
from serving.adapters.provider_registry import RuntimeProviderDefinition
from serving.audit import AuditTarget
from serving.config.route_states import RouteState, RouteStateResolver
from serving.config.routewise_model_settings import model_routewise_setting_keys
from serving.config.settings import get_settings
from serving.pricing import PricingSchedule, effective_pricing
from serving.servers.bootstrap import _EffectiveRouteRefreshState, _reload_effective_route_state
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


def test_extensions_can_register_replace_and_remove_provider_targets(monkeypatch):
    targets = provider_routes.PROVIDER_TARGETS
    target = provider_routes.ProviderTarget(
        provider="extension",
        label="Extension",
        kind="openai_compat",
        key_provider="extension",
        default_base_url="https://extension.example/v1",
    )
    monkeypatch.setitem(targets, "extension", target)
    assert targets["extension"] == target
    assert dict(targets)["extension"] == target
    replacement = provider_routes.replace(target, label="Updated")
    targets.update({"extension": replacement})
    assert targets["extension"] == replacement
    del targets["extension"]
    assert "extension" not in targets


def test_extension_target_overrides_keep_dynamic_base_url_settings(monkeypatch):
    targets = provider_routes.PROVIDER_TARGETS
    custom = provider_routes.replace(
        targets["minimax"], default_base_url="https://extension.example/v1"
    )
    monkeypatch.setattr(provider_routes, "config_value", lambda key: None)
    monkeypatch.setitem(targets, "minimax", custom)
    assert targets["minimax"] == custom
    monkeypatch.setattr(provider_routes, "config_value", lambda key: "https://db.example/v1")
    assert targets["minimax"].default_base_url == "https://db.example/v1"


NOW = datetime(2026, 6, 16, tzinfo=UTC)
# Inside the scheduled peak window, after its activation timestamp.
_PEAK_INSTANT = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)
RUNTIME_PRICING = {
    "prompt": "0.14",
    "completion": "0.28",
    "input_cache_reads": "0.0028",
    "input_cache_writes": "0",
}
RUNTIME_CAPABILITIES = {
    "context_length": 131072,
    "max_output_length": 65536,
    "input_modalities": ["text", "image"],
    "supports_tools": True,
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
    # Capability overrides behave like the table: the sync after an edit reads
    # back what the edit wrote, as JSON text the way asyncpg returns JSONB.
    capability_rows: dict[tuple[str, str], dict] = {}

    async def upsert_capability_override(model_id, route_id, capabilities, updated_by):
        capability_rows[(model_id, route_id)] = {
            "model_id": model_id,
            "route_id": route_id,
            "capabilities": json.dumps(capabilities),
            "updated_by": updated_by,
        }

    async def delete_capability_override(model_id, route_id):
        return capability_rows.pop((model_id, route_id), None) is not None

    async def list_capability_overrides_for_model(model_id):
        return [row for (owner, _), row in sorted(capability_rows.items()) if owner == model_id]

    async def list_all_capability_overrides():
        return [row for _, row in sorted(capability_rows.items())]

    op_store.capability_rows = capability_rows
    op_store.list_route_capability_overrides_for_model = AsyncMock(
        side_effect=list_capability_overrides_for_model
    )
    op_store.list_all_route_capability_overrides = AsyncMock(
        side_effect=list_all_capability_overrides
    )
    op_store.upsert_route_capability_override = AsyncMock(side_effect=upsert_capability_override)
    op_store.delete_route_capability_override = AsyncMock(side_effect=delete_capability_override)
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

    route_executor = FixedRouter()
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

    def clear_router_override(model_id):
        strategy_state.pop(model_id, None)
        router_cache.pop(model_id, None)

    registry.get_router = MagicMock(side_effect=get_router)
    registry.prepare_router_strategy_change = MagicMock(side_effect=prepare_router_strategy_change)
    registry.commit_router_strategy_change = MagicMock(side_effect=commit_router_strategy_change)
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
async def test_get_provider_routes_lists_routewise_candidates(admin_client, monkeypatch):
    client, _op_store, _router, fake_routewise, _verify_mock = admin_client
    for var in ("CHUTES_API_KEY", "FEATHERLESS_API_KEY", "MINIMAX_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    quota_reset_at = datetime(2026, 6, 17, tzinfo=UTC)
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
    offered = {option["provider"] for option in payload["provider_options"]}
    # The selector holds the vendors this deployment routes through, not a
    # fixed list: minimax is a built-in kind with no route or key here.
    assert {"chutes", "featherless", "openrouter"} <= offered
    assert "minimax" not in offered
    chutes_option = next(
        option for option in payload["provider_options"] if option["provider"] == "chutes"
    )
    assert chutes_option["default_base_url"] == "https://llm.chutes.ai/v1"
    assert chutes_option["route_types"] == ["on_demand", "quota", "concurrency"]
    # Pin choices come from the deployment's own OpenRouter routes.
    assert payload["openrouter_provider_options"] == [
        {"provider": "deepinfra", "label": "Deepinfra"}
    ]
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


@pytest.mark.asyncio
async def test_provider_options_offer_a_vendor_with_a_configured_env_key(
    admin_client,
    monkeypatch,
):
    client, _op_store, _router, _fake_routewise, _verify_mock = admin_client
    monkeypatch.setenv("MINIMAX_API_KEY", "minimax-env-key-1234567890")

    response = await client.get("/admin/routing/provider-routes/minimax-fast", headers=AUTH)

    assert response.status_code == 200
    minimax_option = next(
        option for option in response.json()["provider_options"] if option["provider"] == "minimax"
    )
    assert minimax_option["default_base_url"] == "https://api.minimax.io/v1"
    assert minimax_option["route_types"] == ["on_demand", "quota", "concurrency"]


@pytest.mark.asyncio
async def test_provider_options_carry_the_deployment_route_type_policy(
    admin_client,
    monkeypatch,
):
    client, _op_store, _router, _fake_routewise, _verify_mock = admin_client
    monkeypatch.setenv("PROVIDER_ROUTE_TYPES", "chutes=quota,openrouter=concurrency|on_demand")
    get_settings.cache_clear()

    response = await client.get("/admin/routing/provider-routes/minimax-fast", headers=AUTH)

    assert response.status_code == 200
    by_provider = {
        option["provider"]: option["route_types"] for option in response.json()["provider_options"]
    }
    assert by_provider["chutes"] == ["quota"]
    assert by_provider["openrouter"] == ["on_demand", "concurrency"]
    assert by_provider["featherless"] == ["on_demand", "quota", "concurrency"]


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
        ("minimax/fp8", "Minimax Fp8"),
        ("minimax/highspeed", "Minimax Highspeed"),
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
        "pricing_source": {"provider": "deepseek", "model": "deepseek-flash"},
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
    assert cfg["pricing_source"] is None


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
        "pricing_source": {"provider": "deepseek", "model": "deepseek-flash"},
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
    assert "pricing_source" not in cfg
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
        "admin-token",
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
    assert audit.await_args.kwargs["action"] == "routing.provider_routes.strategy.update"
    # Run after the request was cancelled, the deferred audit still names who
    # was admitted: the shared token, which is no account.
    actor = audit.await_args.kwargs["actor"]
    assert actor.type == "admin_token"
    assert actor.id is None


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
    assert registry.get_router_name("on-demand-only") == "routewise"
    assert services.managed_routers == [routewise_router]
    assert not hasattr(routewise_router, "_model_router_override_id")
    assert not hasattr(routewise_router, "_model_router_fallback_strategy")

    await provider_routes._apply_model_router_strategy(
        services,
        "on-demand-only",
        "fixed",
    )

    assert registry.get_router("on-demand-only") is route_executor
    assert registry.get_router_name("on-demand-only") == "fixed"
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
            "key": "routewise_budget_alpha",
            "value": "0.5",
            "value_type": "float",
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
        "admin-token",
        deployment_type="auto",
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
        "admin-token",
        deployment_type="auto",
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
        "admin-token",
        deployment_type="auto",
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
        "admin-token",
        deployment_type="auto",
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
        "admin-token",
        deployment_type="auto",
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
            "base_url": "http://169.254.169.254/latest",
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
async def test_put_provider_route_rejects_metadata_dns_base_url(admin_client, monkeypatch):
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
                ("169.254.169.254", port or 443),
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
            "capabilities": RUNTIME_CAPABILITIES,
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
        RUNTIME_CAPABILITIES,
        "admin-token",
        deployment_type="auto",
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
            "capabilities": RUNTIME_CAPABILITIES,
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
        RUNTIME_CAPABILITIES,
        "admin-token",
        deployment_type="auto",
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
async def test_runtime_candidate_pricing_schedule_survives_restart(admin_client, caplog):
    """A runtime route on a scheduled YAML model prices the same before and after a restart.

    ``upsert_provider_route_candidate`` persists no schedule, but restore for a
    model the router already knows re-runs ``_prepare_route_candidate``, which
    re-clones the YAML template. Inheriting the template schedule is therefore
    what keeps the freshly created route and the rehydrated one in agreement —
    dropping it at creation would make the live route ignore the provider's
    time-of-day pricing that its YAML sibling still honors.
    """
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.list_provider_keys_full.return_value = ["minimax-db-key-1234567890"]
    schedule = PricingSchedule.from_raw(
        {
            "effective_at": "2026-08-16T16:00:00Z",
            "timezone": "UTC",
            "weekdays": [1, 2, 3, 4, 5],
            "excluded_dates": ["2026-08-18"],
            "default": {"prompt": "0.22", "completion": "0.66"},
            "windows": [{"start": "01:00", "end": "04:00", "pricing": {"prompt": "0.44"}}],
        }
    )
    for entry in route_executor.routes["minimax-fast"].raw_adapters:
        entry[0].config.pricing_schedule = schedule

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "on_demand",
            "upstream_provider": "minimax",
            "base_url": "https://api.minimax.io/v1",
            "provider_model_id": "MiniMax-M2.5",
            "weight": 2.0,
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    created_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert created_adapter.config.route_metadata["runtime_candidate"] is True

    persisted = op_store.upsert_provider_route_candidate.await_args.args
    route_id = persisted[1]
    op_store.list_all_provider_route_candidates.return_value = [
        {
            "model_id": "minimax-fast",
            "route_id": route_id,
            "route_type": persisted[2],
            "provider": persisted[3],
            "openrouter_sort": persisted[4],
            "base_url": persisted[5],
            "api_key_id": persisted[6],
            "provider_model_id": persisted[7],
            "quota_limit": persisted[8],
            "concurrency_limit": persisted[9],
            "weight": persisted[10],
            "pricing": persisted[11],
            "capabilities": persisted[12],
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
    # Drop the live route so restore rebuilds it from the persisted row. Without
    # this the route_id is still installed, restore raises 409, and
    # apply_persisted_provider_route_candidates swallows it into a warning —
    # leaving the assertions below comparing the created adapter with itself.
    provider_routes._install_route_candidate_delete(
        services,
        route_executor.routes["minimax-fast"],
        route_id,
    )
    assert all(entry[2] != route_id for entry in route_executor.routes["minimax-fast"].raw_adapters)

    with caplog.at_level(logging.WARNING, logger=provider_routes.logger.name):
        await apply_persisted_provider_route_candidates(services, op_store)

    assert "Failed to apply provider route candidate" not in caplog.text
    restored_adapter = next(
        entry[0]
        for entry in route_executor.routes["minimax-fast"].raw_adapters
        if entry[2] == route_id
    )
    assert restored_adapter is not created_adapter
    assert restored_adapter.config.pricing == created_adapter.config.pricing
    assert restored_adapter.config.pricing_schedule == created_adapter.config.pricing_schedule
    assert restored_adapter.config.pricing_schedule == schedule
    assert effective_pricing(restored_adapter.config, at=_PEAK_INSTANT) == effective_pricing(
        created_adapter.config, at=_PEAK_INSTANT
    )
    assert (
        effective_pricing(restored_adapter.config, at=datetime(2026, 8, 18, 2, tzinfo=UTC))[
            "prompt"
        ]
        == "0.22"
    )


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
            "capabilities": RUNTIME_CAPABILITIES,
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
        RUNTIME_CAPABILITIES,
        "admin-token",
        deployment_type="auto",
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
            "capabilities": RUNTIME_CAPABILITIES,
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


class _CandidateTable:
    """Stateful stand-in for the candidate and weight-override tables.

    The fixture's store is a bare mock, but an edit reads the row it replaces
    and writes it back by compare-and-swap, so these tests need rows that
    persist between calls and answer the way PostgresOperationalStore does.
    """

    def __init__(self, op_store) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.weights: dict[tuple[str, str], float] = {}
        self._tick = 0
        op_store.upsert_provider_route_candidate = AsyncMock(side_effect=self.upsert)
        op_store.update_provider_route_candidate = AsyncMock(side_effect=self.update)
        op_store.list_provider_route_candidates_for_model = AsyncMock(
            side_effect=self.list_for_model
        )
        op_store.list_all_provider_route_candidates = AsyncMock(side_effect=self.list_all)
        op_store.delete_provider_route_candidate_with_config = AsyncMock(side_effect=self.delete)

    def _now(self) -> datetime:
        self._tick += 1
        return NOW.replace(microsecond=self._tick)

    async def upsert(
        self,
        model_id,
        route_id,
        route_type,
        provider,
        openrouter_sort,
        base_url,
        api_key_id,
        provider_model_id,
        quota_limit,
        concurrency_limit,
        weight,
        pricing,
        capabilities,
        updated_by,
        deployment_type="auto",
        endpoint_id=None,
    ) -> None:
        stored = self.rows.get((model_id, route_id), {})
        self.rows[(model_id, route_id)] = {
            "model_id": model_id,
            "route_id": route_id,
            "route_type": route_type,
            "provider": provider,
            "openrouter_sort": openrouter_sort,
            "base_url": base_url,
            "api_key_id": api_key_id,
            "provider_model_id": provider_model_id,
            "quota_limit": quota_limit,
            "concurrency_limit": concurrency_limit,
            "weight": weight,
            "pricing": json.dumps(pricing) if pricing is not None else None,
            "capabilities": (
                json.dumps(capabilities) if capabilities is not None else stored.get("capabilities")
            ),
            "deployment_type": deployment_type,
            "endpoint_id": endpoint_id,
            "created_at": stored.get("created_at", NOW),
            "updated_at": self._now(),
            "updated_by": updated_by,
        }

    async def update(
        self,
        model_id,
        route_id,
        *,
        expected_updated_at,
        provider,
        openrouter_sort,
        base_url,
        api_key_id,
        provider_model_id,
        quota_limit,
        concurrency_limit,
        pricing,
        deployment_type,
        endpoint_id,
        previous_endpoint_id,
        updated_by,
    ):
        row = self.rows.get((model_id, route_id))
        if row is None or row["updated_at"] != expected_updated_at:
            return None
        row.update(
            provider=provider,
            openrouter_sort=openrouter_sort,
            base_url=base_url,
            api_key_id=api_key_id,
            provider_model_id=provider_model_id,
            quota_limit=quota_limit,
            concurrency_limit=concurrency_limit,
            pricing=json.dumps(pricing) if pricing is not None else None,
            deployment_type=deployment_type,
            endpoint_id=endpoint_id,
            updated_at=self._now(),
            updated_by=updated_by,
        )
        new_endpoint_id = endpoint_id or route_id
        if new_endpoint_id != previous_endpoint_id:
            self.weights.pop((model_id, new_endpoint_id), None)
            weight = self.weights.pop((model_id, previous_endpoint_id), None)
            if weight is not None:
                self.weights[(model_id, new_endpoint_id)] = weight
        return row["updated_at"]

    async def list_for_model(self, model_id):
        return [dict(row) for key, row in sorted(self.rows.items()) if key[0] == model_id]

    async def list_all(self):
        return [dict(row) for _key, row in sorted(self.rows.items())]

    async def delete(self, model_id, route_id):
        return self.rows.pop((model_id, route_id), None) is not None

    def row(self, model_id: str, route_id: str) -> dict[str, Any]:
        return dict(self.rows[(model_id, route_id)])


MINIMAX_ROUTE_ID = "minimax-fast:minimax-api"
PARASAIL_ENDPOINT_ID = "minimax-fast:openrouter[parasail]-api"
OPENROUTER_PARASAIL_TARGET = {
    "upstream_provider": "openrouter",
    "openrouter_provider": "parasail",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_id": "db-openrouter",
    "provider_model_id": "minimax/minimax-m2.5",
}


def _attach_weight_resolver(client, route_executor, table: _CandidateTable):
    """Give the app a real weight-override resolver whose store is ``table``."""
    from serving.config.weight_overrides import WeightOverrideResolver

    store = SimpleNamespace(
        list_all_weight_overrides=AsyncMock(
            side_effect=lambda: [
                {"model_id": model_id, "endpoint_id": endpoint_id, "weight": weight}
                for (model_id, endpoint_id), weight in table.weights.items()
            ]
        )
    )
    resolver = WeightOverrideResolver(store)
    client.app.state.services.weight_override_resolver = resolver
    route_executor.weight_override_resolver = resolver
    return resolver


async def _add_minimax_runtime_route(client, op_store, **overrides) -> dict[str, Any]:
    op_store.list_provider_keys_full.return_value = ["minimax-db-key-1234567890"]
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    body = {
        "capabilities": RUNTIME_CAPABILITIES,
        "route_type": "on_demand",
        "upstream_provider": "minimax",
        "base_url": "https://api.minimax.io/v1",
        "provider_model_id": "MiniMax-M2.5",
        "weight": 2.0,
    }
    body.update(overrides)
    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json=body,
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _live_entry(route_executor, route_id: str, model_id: str = "minimax-fast"):
    entries = route_executor.routes[model_id].raw_adapters
    for index, (adapter, weight, endpoint_id) in enumerate(entries):
        if adapter.config.route_metadata.get("route_id") == route_id:
            return index, adapter, weight, endpoint_id
    raise AssertionError(f"route {route_id} is not live")


async def _patch_candidate(client, route_id: str, body: dict, model_id: str = "minimax-fast"):
    return await client.patch(
        f"/admin/routing/provider-route-candidates/{model_id}/{route_id}",
        json=body,
        headers=AUTH,
    )


@pytest.mark.asyncio
async def test_patch_provider_route_candidate_updates_openrouter_concurrency_limit(
    admin_client,
):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    create_response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "concurrency",
            **OPENROUTER_PARASAIL_TARGET,
            "concurrency_limit": 2,
            "weight": 1.0,
        },
        headers=AUTH,
    )
    assert create_response.status_code == 200, create_response.text
    fake_routewise.refresh_route_table.reset_mock()
    verify_mock.reset_mock()
    created_at = table.row("minimax-fast", PARASAIL_ENDPOINT_ID)["updated_at"]

    # The inline limit editor still sends the limit alone.
    response = await _patch_candidate(client, PARASAIL_ENDPOINT_ID, {"concurrency_limit": 4})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["route_type"] == "concurrency"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["concurrency_limit"] == 4
    assert payload["endpoint_id"] == PARASAIL_ENDPOINT_ID
    # In place: one UPDATE of the existing row, no delete and re-create.
    op_store.update_provider_route_candidate.assert_awaited_once()
    op_store.delete_provider_route_candidate_with_config.assert_not_awaited()
    row = table.row("minimax-fast", PARASAIL_ENDPOINT_ID)
    assert row["concurrency_limit"] == 4
    assert row["updated_at"] > created_at
    assert row["provider"] == "openrouter[parasail]"
    assert row["api_key_id"] == "db-openrouter"
    # The route keeps the capabilities it declared when it was added.
    assert json.loads(row["capabilities"]) == RUNTIME_CAPABILITIES
    assert payload["capabilities"] == RUNTIME_CAPABILITIES
    # A limit does not change where requests go, so nothing is sent upstream.
    verify_mock.assert_not_awaited()
    _index, runtime_adapter, _weight, _endpoint = _live_entry(route_executor, PARASAIL_ENDPOINT_ID)
    assert runtime_adapter.config.concurrency == {"limit": 4}
    assert runtime_adapter.config.concurrency_pool == (
        f"{PARASAIL_ENDPOINT_ID}:runtime-concurrency"
    )
    fake_routewise.refresh_route_table.assert_called_once_with()


@pytest.mark.asyncio
async def test_patch_provider_route_candidate_rejects_config_route(admin_client):
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    _CandidateTable(op_store)

    response = await _patch_candidate(
        client, "minimax-fast:featherless-api", {"concurrency_limit": 4}
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "Only runtime-added provider routes can be edited as candidates"
    )
    op_store.update_provider_route_candidate.assert_not_awaited()
    verify_mock.assert_not_awaited()
    featherless_adapter = route_executor.routes["minimax-fast"].raw_adapters[1][0]
    assert featherless_adapter.config.concurrency == {"limit": 1}


@pytest.mark.asyncio
async def test_runtime_route_edit_retargets_in_place_and_keeps_identity_and_weight(
    admin_client,
):
    """Moving a runtime route keeps its route id, position and weight, and survives a restart."""
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    resolver = _attach_weight_resolver(client, route_executor, table)
    await _add_minimax_runtime_route(client, op_store)
    index, old_adapter, _weight, old_endpoint_id = _live_entry(route_executor, MINIMAX_ROUTE_ID)
    assert old_endpoint_id == MINIMAX_ROUTE_ID
    # An admin-set runtime weight for the route, keyed by its endpoint id.
    table.weights[("minimax-fast", MINIMAX_ROUTE_ID)] = 5.0
    resolver.set_override("minimax-fast", MINIMAX_ROUTE_ID, 5.0)
    verify_mock.reset_mock()
    fake_routewise.refresh_route_table.reset_mock()
    openrouter_pools = len(dynamic_keys.get_pools_for_provider("openrouter"))

    response = await _patch_candidate(
        client,
        MINIMAX_ROUTE_ID,
        {**OPENROUTER_PARASAIL_TARGET, "deployment_type": "provider_api"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["route_id"] == MINIMAX_ROUTE_ID
    assert payload["endpoint_id"] == PARASAIL_ENDPOINT_ID
    assert payload["source"] == "runtime"
    assert payload["upstream_provider"] == "openrouter"
    assert payload["openrouter_provider"] == "parasail"
    assert payload["key_provider"] == "openrouter"
    assert payload["api_key_id"] == "db-openrouter"
    assert payload["yaml_weight"] == 2.0
    assert payload["effective_weight"] == 5.0
    assert payload["deployment_type"] == "provider_api"
    verify_mock.assert_awaited_once()

    new_index, new_adapter, raw_weight, endpoint_id = _live_entry(route_executor, MINIMAX_ROUTE_ID)
    assert new_index == index
    assert new_adapter is not old_adapter
    assert raw_weight == 2.0
    assert endpoint_id == PARASAIL_ENDPOINT_ID
    assert new_adapter.config.endpoint_id == PARASAIL_ENDPOINT_ID
    assert new_adapter.config.openrouter_pinned_provider == "parasail"
    assert new_adapter.config.pricing == OPENROUTER_PARASAIL_PRICING
    assert new_adapter.config.route_metadata["runtime_candidate"] is True
    assert new_adapter.config.route_metadata["api_key_id"] == "db-openrouter"
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == openrouter_pools + 1
    fake_routewise.refresh_route_table.assert_called_once_with()

    row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    assert row["provider"] == "openrouter[parasail]"
    assert row["endpoint_id"] == PARASAIL_ENDPOINT_ID
    assert row["api_key_id"] == "db-openrouter"
    assert row["deployment_type"] == "provider_api"
    assert row["weight"] == 2.0
    assert table.weights == {("minimax-fast", PARASAIL_ENDPOINT_ID): 5.0}
    assert resolver.get_snapshot_for_model("minimax-fast") == {PARASAIL_ENDPOINT_ID: 5.0}

    provider_routes.log_admin_action.assert_awaited()
    audit = provider_routes.log_admin_action.await_args.kwargs
    action, details = audit["action"], audit["details"]
    assert action == "routing.provider_routes.candidate.update"
    # A route belongs to its model: the model is the typed target, never a user.
    assert audit["target"] == AuditTarget.model("minimax-fast")
    assert audit["target"].user_id is None
    assert details["old_endpoint_id"] == MINIMAX_ROUTE_ID
    assert details["endpoint_id"] == PARASAIL_ENDPOINT_ID
    assert details["old_upstream_provider"] == "minimax"
    assert details["upstream_provider"] == "openrouter[parasail]"

    # Restart: drop the live route and rebuild it from the stored row.
    services = client.app.state.services
    provider_routes._install_route_candidate_delete(
        services, route_executor.routes["minimax-fast"], MINIMAX_ROUTE_ID
    )
    await apply_persisted_provider_route_candidates(services, op_store)

    _index, restored, restored_weight, restored_endpoint = _live_entry(
        route_executor, MINIMAX_ROUTE_ID
    )
    assert restored is not new_adapter
    assert restored_endpoint == PARASAIL_ENDPOINT_ID
    assert restored_weight == 2.0
    for field in (
        "provider",
        "base_url",
        "provider_model_id",
        "endpoint_id",
        "deployment_type",
        "pricing",
        "openrouter_pinned_provider",
    ):
        assert getattr(restored.config, field) == getattr(new_adapter.config, field), field
    assert restored.config.route_metadata["route_id"] == MINIMAX_ROUTE_ID


@pytest.mark.asyncio
async def test_runtime_route_edit_back_to_its_first_target_restores_the_route_id_endpoint(
    admin_client,
):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    moved = await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)
    assert moved.status_code == 200, moved.text

    back = await _patch_candidate(
        client,
        MINIMAX_ROUTE_ID,
        {
            "upstream_provider": "minimax",
            "base_url": "https://api.minimax.io/v1",
            "provider_model_id": "MiniMax-M2.5",
        },
    )

    assert back.status_code == 200, back.text
    assert back.json()["endpoint_id"] == MINIMAX_ROUTE_ID
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID)["endpoint_id"] is None
    assert _live_entry(route_executor, MINIMAX_ROUTE_ID)[3] == MINIMAX_ROUTE_ID


@pytest.mark.asyncio
async def test_runtime_route_edit_without_a_new_target_keeps_its_endpoint(admin_client):
    """A key change keeps the endpoint id; a deployment-type change alone is not verified."""
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    op_store.get_provider_key_full.return_value = ("minimax", "minimax-db-key-0987654321")
    verify_mock.reset_mock()

    key_edit = await _patch_candidate(
        client,
        MINIMAX_ROUTE_ID,
        {
            "upstream_provider": "minimax",
            "base_url": "https://api.minimax.io/v1/",
            "api_key_id": "db-minimax",
            "provider_model_id": "MiniMax-M2.5",
        },
    )
    assert key_edit.status_code == 200, key_edit.text
    assert key_edit.json()["endpoint_id"] == MINIMAX_ROUTE_ID
    verify_mock.assert_awaited_once()
    row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    assert row["api_key_id"] == "db-minimax"
    # The same endpoint keeps its registered URL verbatim.
    assert row["base_url"] == "https://api.minimax.io/v1"
    assert row["endpoint_id"] is None

    verify_mock.reset_mock()
    deployment_edit = await _patch_candidate(
        client, MINIMAX_ROUTE_ID, {"deployment_type": "self_hosted"}
    )
    assert deployment_edit.status_code == 200, deployment_edit.text
    assert deployment_edit.json()["deployment_type"] == "self_hosted"
    verify_mock.assert_not_awaited()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID)["api_key_id"] == "db-minimax"
    assert _live_entry(route_executor, MINIMAX_ROUTE_ID)[1].config.deployment_type == "self_hosted"


@pytest.mark.asyncio
async def test_runtime_route_edit_resets_deployment_type_when_it_moves(admin_client):
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client
    _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store, deployment_type="self_hosted")

    response = await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)

    assert response.status_code == 200, response.text
    assert response.json()["deployment_type"] == "auto"


@pytest.mark.asyncio
async def test_runtime_route_edit_verify_failure_changes_nothing(admin_client):
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    before_row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    before_entries = list(route_executor.routes["minimax-fast"].raw_adapters)
    fake_routewise.refresh_route_table.reset_mock()
    openrouter_pools = len(dynamic_keys.get_pools_for_provider("openrouter"))
    verify_mock.side_effect = HTTPException(
        status_code=400, detail="Provider verification failed with HTTP 401"
    )

    response = await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)

    assert response.status_code == 400
    assert response.json()["detail"] == "Provider verification failed with HTTP 401"
    op_store.update_provider_route_candidate.assert_not_awaited()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID) == before_row
    assert route_executor.routes["minimax-fast"].raw_adapters == before_entries
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == openrouter_pools
    fake_routewise.refresh_route_table.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_route_edit_save_failure_leaves_the_live_route(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    before_row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    before_entries = list(route_executor.routes["minimax-fast"].raw_adapters)
    fake_routewise.refresh_route_table.reset_mock()
    provider_routes.log_admin_action.reset_mock()
    op_store.update_provider_route_candidate.side_effect = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)

    assert table.row("minimax-fast", MINIMAX_ROUTE_ID) == before_row
    assert route_executor.routes["minimax-fast"].raw_adapters == before_entries
    fake_routewise.refresh_route_table.assert_not_called()
    provider_routes.log_admin_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_route_edit_live_failure_restores_the_row_and_weight(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    resolver = _attach_weight_resolver(client, route_executor, table)
    await _add_minimax_runtime_route(client, op_store)
    table.weights[("minimax-fast", MINIMAX_ROUTE_ID)] = 5.0
    resolver.set_override("minimax-fast", MINIMAX_ROUTE_ID, 5.0)
    before_row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    before_entries = list(route_executor.routes["minimax-fast"].raw_adapters)
    openrouter_pools = len(dynamic_keys.get_pools_for_provider("openrouter"))
    fake_routewise.refresh_route_table.reset_mock()
    provider_routes.log_admin_action.reset_mock()
    fake_routewise.refresh_route_table.side_effect = [RuntimeError("rebuild failed"), None]

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)

    restored_row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    for field in (
        "provider",
        "base_url",
        "api_key_id",
        "provider_model_id",
        "deployment_type",
        "endpoint_id",
        "pricing",
    ):
        assert restored_row[field] == before_row[field], field
    assert table.weights == {("minimax-fast", MINIMAX_ROUTE_ID): 5.0}
    assert resolver.get_snapshot_for_model("minimax-fast") == {MINIMAX_ROUTE_ID: 5.0}
    assert route_executor.routes["minimax-fast"].raw_adapters == before_entries
    assert len(dynamic_keys.get_pools_for_provider("openrouter")) == openrouter_pools
    assert op_store.update_provider_route_candidate.await_count == 2
    provider_routes.log_admin_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_route_edit_refuses_another_routes_endpoint(admin_client):
    """Moving onto an endpoint another route of the model uses would merge their state."""
    client, op_store, _route_executor, _fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    before_row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    verify_mock.reset_mock()

    response = await _patch_candidate(
        client,
        MINIMAX_ROUTE_ID,
        {
            "upstream_provider": "featherless",
            "base_url": "https://api.featherless.ai/v1",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "another route of this model already uses endpoint id 'minimax-fast:featherless-api'"
    )
    verify_mock.assert_not_awaited()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID) == before_row


@pytest.mark.asyncio
async def test_new_route_cannot_take_an_edited_routes_endpoint(admin_client):
    """A route added later is refused the endpoint id an edited route moved onto."""
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client
    _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    moved = await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)
    assert moved.status_code == 200, moved.text

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "on_demand",
            **OPENROUTER_PARASAIL_TARGET,
            "weight": 1.0,
        },
        headers=AUTH,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "provider route already exists"


@pytest.mark.asyncio
async def test_runtime_route_edit_refuses_a_row_changed_by_another_process(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    before_entries = list(route_executor.routes["minimax-fast"].raw_adapters)
    # Another gateway process moved the route; this one still serves the old copy.
    table.rows[("minimax-fast", MINIMAX_ROUTE_ID)]["provider_model_id"] = "MiniMax-M2.7"

    response = await _patch_candidate(client, MINIMAX_ROUTE_ID, {"deployment_type": "self_hosted"})

    assert response.status_code == 409, response.text
    assert "changed from another gateway process" in response.json()["detail"]
    op_store.update_provider_route_candidate.assert_not_awaited()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID)["provider_model_id"] == "MiniMax-M2.7"
    assert route_executor.routes["minimax-fast"].raw_adapters == before_entries


@pytest.mark.asyncio
async def test_runtime_route_edit_loses_a_compare_and_swap_race(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    before_entries = list(route_executor.routes["minimax-fast"].raw_adapters)
    fake_routewise.refresh_route_table.reset_mock()

    async def concurrent_write(*_args, **_kwargs):
        # Another writer landed between this edit's read and its write.
        return None

    op_store.update_provider_route_candidate.side_effect = concurrent_write

    response = await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "provider route candidate changed concurrently; reload and retry"
    )
    assert route_executor.routes["minimax-fast"].raw_adapters == before_entries
    fake_routewise.refresh_route_table.assert_not_called()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID)["provider"] == "minimax"


@pytest.mark.asyncio
async def test_runtime_route_edit_refuses_a_route_changed_during_verification(admin_client):
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    route = route_executor.routes["minimax-fast"]

    async def concurrent_edit(_candidate):
        # A second edit finishes while this one is talking to the upstream.
        index, adapter, weight, endpoint_id = _live_entry(route_executor, MINIMAX_ROUTE_ID)
        entries = list(route.raw_adapters)
        entries[index] = (
            provider_routes._make_adapter(
                "minimax", {**provider_routes._config_to_dict(adapter.config)}
            ),
            weight,
            endpoint_id,
        )
        route.raw_adapters = entries

    verify_mock.side_effect = concurrent_edit
    before_row = table.row("minimax-fast", MINIMAX_ROUTE_ID)

    response = await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "provider route changed during verification"
    op_store.update_provider_route_candidate.assert_not_awaited()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID) == before_row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (
            {**OPENROUTER_PARASAIL_TARGET, "base_url": "http://169.254.169.254/v1"},
            422,
            "base_url host is not allowed",
        ),
        (
            {
                "upstream_provider": "minimax",
                "base_url": "https://openrouter.ai/api/v1",
                "provider_model_id": "MiniMax-M2.5",
            },
            422,
            "base_url host belongs to provider 'openrouter', not 'minimax'",
        ),
        (
            {
                "upstream_provider": "minimax",
                "base_url": "https://api.minimax.io/v1",
                "api_key_id": "db-openrouter",
                "provider_model_id": "MiniMax-M2.5",
            },
            400,
            "Provider key belongs to 'openrouter', expected 'minimax'",
        ),
        ({"quota_limit": 10}, 422, "quota_limit is only valid for quota routes"),
        (
            {"base_url": "https://api.minimax.io/v2"},
            422,
            "upstream_provider is required to change a route's target",
        ),
        (
            {"upstream_provider": "minimax", "base_url": "https://api.minimax.io/v1"},
            422,
            "base_url and provider_model_id are required with upstream_provider",
        ),
    ],
    ids=[
        "metadata-host",
        "other-providers-host",
        "other-providers-key",
        "limit-type",
        "partial",
        "incomplete-target",
    ],
)
async def test_runtime_route_edit_validates_on_the_server(admin_client, body, status, detail):
    client, op_store, _route_executor, _fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    before_row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    verify_mock.reset_mock()

    response = await _patch_candidate(client, MINIMAX_ROUTE_ID, body)

    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail
    verify_mock.assert_not_awaited()
    op_store.update_provider_route_candidate.assert_not_awaited()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID) == before_row


@pytest.mark.asyncio
async def test_runtime_route_edit_requires_admin(admin_client):
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client
    _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)

    response = await client.patch(
        f"/admin/routing/provider-route-candidates/minimax-fast/{MINIMAX_ROUTE_ID}",
        json={"deployment_type": "self_hosted"},
        headers={"Authorization": "Bearer not-the-admin-token"},
    )

    assert response.status_code in {401, 403}
    op_store.update_provider_route_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_quota_route_edit_changes_its_own_limit(admin_client):
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store, route_type="quota", quota_limit=100)
    verify_mock.reset_mock()

    response = await _patch_candidate(client, MINIMAX_ROUTE_ID, {"quota_limit": 250})

    assert response.status_code == 200, response.text
    assert response.json()["quota_limit"] == 250
    verify_mock.assert_not_awaited()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID)["quota_limit"] == 250
    adapter = _live_entry(route_executor, MINIMAX_ROUTE_ID)[1]
    assert adapter.config.quota == {"limit": 250}
    # The local quota pool is the route's, so it keeps its id (and its usage).
    assert adapter.config.quota_pool == f"{MINIMAX_ROUTE_ID}:runtime-quota"


@pytest.mark.asyncio
async def test_verify_runtime_route_edit_does_not_apply(admin_client):
    """The edit form's Verify builds a runtime route's edit as Apply would, and saves nothing."""
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    before_row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    before_entries = list(route_executor.routes["minimax-fast"].raw_adapters)
    verify_mock.reset_mock()
    fake_routewise.refresh_route_table.reset_mock()

    response = await client.post(
        f"/admin/routing/provider-route-verifications/minimax-fast/{MINIMAX_ROUTE_ID}",
        json=OPENROUTER_PARASAIL_TARGET,
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    verify_mock.assert_awaited_once()
    (candidate,) = verify_mock.await_args.args
    assert candidate.adapter.config.openrouter_pinned_provider == "parasail"
    assert candidate.endpoint_id == PARASAIL_ENDPOINT_ID
    assert candidate.route_id == MINIMAX_ROUTE_ID
    op_store.update_provider_route_candidate.assert_not_awaited()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID) == before_row
    assert route_executor.routes["minimax-fast"].raw_adapters == before_entries
    fake_routewise.refresh_route_table.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_model_first_route_edit_keeps_its_declared_pricing(admin_client):
    """The route a runtime model was created from is rebuilt from its own row, as at boot."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    create = await client.post(
        "/admin/routing/provider-route-models",
        json=_runtime_model_payload(),
        headers=AUTH,
    )
    assert create.status_code == 200, create.text
    route_id = create.json()["route_id"]

    response = await _patch_candidate(
        client,
        route_id,
        {
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-pro",
        },
        model_id="deepseek-v4-flash",
    )

    assert response.status_code == 200, response.text
    assert response.json()["route_id"] == route_id
    adapter = _live_entry(route_executor, route_id, model_id="deepseek-v4-flash")[1]
    assert adapter.config.provider_model_id == "deepseek/deepseek-v4-pro"
    assert adapter.config.openrouter_pinned_provider == "parasail"
    assert adapter.config.pricing == RUNTIME_PRICING
    _assert_capabilities(adapter, RUNTIME_CAPABILITIES, declared=True)
    row = table.row("deepseek-v4-flash", route_id)
    assert json.loads(row["pricing"]) == RUNTIME_PRICING
    assert json.loads(row["capabilities"]) == RUNTIME_CAPABILITIES


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
            "capabilities": RUNTIME_CAPABILITIES,
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
                "capabilities": RUNTIME_CAPABILITIES,
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
    env_key_id = dynamic_keys.env_key_id(numbered_key)

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
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


@pytest.fixture
def single_static_key_route(admin_client):
    _client, op_store, executor, _router, _verify = admin_client
    provider = "static-service"
    key = "static-service-test-key-1234567890"
    provider_registry.register_provider_definition(
        RuntimeProviderDefinition(
            provider=provider,
            display_name="Static service",
            adapter_kind="openai_compat",
            default_base_url="https://static.example.test/v1",
        )
    )
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="static-model",
            name="Static model",
            provider=provider,
            endpoint_id="static-model:static-api",
            base_url="https://static.example.test/v1",
            api_key=key,
        )
    )
    assert adapter._key_pool is None
    executor.register_route("static-model", [(adapter, 1.0)])
    dynamic_keys.register_adapter_for_provider(provider, adapter)
    op_store.list_provider_env_key_min_roles = AsyncMock(return_value={})
    op_store.list_provider_key_min_roles = AsyncMock(return_value={})
    op_store.list_provider_key_values = AsyncMock(return_value={})
    op_store.list_provider_env_key_reservations = AsyncMock(return_value=[])
    op_store.list_disabled_provider_env_keys = AsyncMock(return_value=[])
    try:
        yield provider, key
    finally:
        dynamic_keys.unregister_adapter_for_provider(provider, adapter)
        provider_registry.unregister_provider_definition(provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_key", [False, True], ids=["default", "explicit"])
async def test_route_edit_resolves_listed_single_static_key(
    admin_client, single_static_key_route, explicit_key
):
    client, op_store, executor, _router, verify_mock = admin_client
    provider, key = single_static_key_route
    listed = await client.get("/admin/provider-keys", params={"provider": provider}, headers=AUTH)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["keys"]
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert key not in listed.text
    key_id = rows[0]["id"] if explicit_key else None
    payload = {
        "upstream_provider": provider,
        "base_url": "https://replacement.example.test/v1",
        "api_key_id": key_id,
        "provider_model_id": "static-model",
    }
    verified = await client.post(
        "/admin/routing/provider-route-verifications/static-model/static-model:static-api",
        json=payload,
        headers=AUTH,
    )
    assert verified.status_code == 200, verified.text
    assert verify_mock.await_args.args[0].adapter.config.api_keys == [key]
    op_store.upsert_provider_route_config.assert_not_awaited()

    async def save_route(*args, **kwargs):
        op_store.list_provider_route_configs_for_model.return_value = [
            {
                "model_id": args[0],
                "route_id": args[1],
                "provider": args[2],
                "base_url": args[4],
                "api_key_id": args[5],
                "provider_model_id": args[6],
                **kwargs,
            }
        ]

    op_store.upsert_provider_route_config.side_effect = save_route
    updated = await client.put(
        "/admin/routing/provider-routes/static-model/static-model:static-api",
        json=payload,
        headers=AUTH,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["api_key"]["source"] == ("env" if explicit_key else "default")
    assert updated.json()["api_key"]["id"] == key_id
    assert key not in updated.text
    assert executor.routes["static-model"].raw_adapters[0][0].config.api_keys == [key]
    op_store.upsert_provider_route_config.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_key", [False, True], ids=["default", "explicit"])
@pytest.mark.parametrize("disabled_source", ["database", "memory"])
async def test_route_edit_rejects_disabled_single_static_key(
    admin_client, single_static_key_route, explicit_key, disabled_source, monkeypatch
):
    client, op_store, _executor, _router, verify_mock = admin_client
    provider, key = single_static_key_route
    key_hash = dynamic_keys.env_key_hash(key)
    if disabled_source == "database":
        op_store.list_disabled_provider_env_key_hashes.return_value = {key_hash}
    else:
        dynamic_keys.disable_env_key_for_provider(provider, key, key_hash)
        # Disabling drops the key from the adapter's pool, but a setting still
        # configures it, so only the in-memory tombstone can keep it out.
        monkeypatch.setattr(
            dynamic_keys,
            "configured_env_keys_for_provider",
            lambda name: [key] if name == provider else [],
        )
    response = await client.put(
        "/admin/routing/provider-routes/static-model/static-model:static-api",
        json={
            "upstream_provider": provider,
            "base_url": "https://replacement.example.test/v1",
            "api_key_id": dynamic_keys.env_key_id(key) if explicit_key else None,
            "provider_model_id": "static-model",
        },
        headers=AUTH,
    )
    assert response.status_code == (404 if explicit_key else 400), response.text
    verify_mock.assert_not_awaited()
    op_store.upsert_provider_route_config.assert_not_awaited()


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
            "capabilities": RUNTIME_CAPABILITIES,
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
        RUNTIME_CAPABILITIES,
        "admin-token",
        deployment_type="auto",
    )
    op_store.set_setting.assert_has_awaits(
        [
            call(
                "model_router_strategy:deepseek-v4-flash",
                "fixed",
                "string",
                "admin-token",
            ),
            call(
                "model_required_role:deepseek-v4-flash",
                "admin",
                "string",
                "admin-token",
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
    assert runtime_adapter.config.context_length == 131072
    assert runtime_adapter.config.max_output_length == 65536
    assert runtime_adapter.config.input_modalities == ["text", "image"]
    assert runtime_adapter.config.supports_tools is True
    assert runtime_adapter.config.route_metadata["declared_capabilities"] is True

    list_response = await client.get(
        "/admin/routing/provider-routes/deepseek-v4-flash",
        headers=AUTH,
    )
    assert list_response.status_code == 200, list_response.text
    route = list_response.json()["routes"][0]
    assert route["api_key_id"] == "db-openrouter"
    assert route["api_key"]["source"] == "db"
    fake_routewise.refresh_route_table.assert_called_once_with()


def _runtime_model_payload(**overrides):
    payload = {
        "model_id": "deepseek-v4-flash",
        "strategy": "fixed",
        "route_type": "on_demand",
        "upstream_provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_id": "db-openrouter",
        "provider_model_id": "deepseek/deepseek-v4-flash",
        "pricing": RUNTIME_PRICING,
        "capabilities": RUNTIME_CAPABILITIES,
    }
    payload.update(overrides)
    return payload


def _capture_upstream_payloads(adapter, monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def post(_url, payload):
        sent.append(payload)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(adapter, "_post_with_pool", post)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        {key: value for key, value in RUNTIME_CAPABILITIES.items() if key != "supports_tools"},
        {**RUNTIME_CAPABILITIES, "input_modalities": ["image"]},
        # Not a modality the request gate holds media to, so declaring it means nothing.
        {**RUNTIME_CAPABILITIES, "input_modalities": ["text", "file"]},
        {**RUNTIME_CAPABILITIES, "context_length": 0},
        {**RUNTIME_CAPABILITIES, "context_length": 8192, "max_output_length": 16384},
        # Lax coercion would read these as 1, 1 (clamping every request to one
        # token), a number, and False.
        {**RUNTIME_CAPABILITIES, "context_length": True, "max_output_length": True},
        {**RUNTIME_CAPABILITIES, "context_length": "131072"},
        {**RUNTIME_CAPABILITIES, "supports_tools": "no"},
        {**RUNTIME_CAPABILITIES, "context_length": 10**20},
    ],
    ids=[
        "missing",
        "tools-unanswered",
        "no-text",
        "unknown-modality",
        "zero-context",
        "output-over-context",
        "boolean-lengths",
        "string-length",
        "string-tools",
        "unbounded-context",
    ],
)
async def test_post_provider_route_model_requires_declared_capabilities(
    admin_client,
    capabilities,
):
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    payload = _runtime_model_payload()
    if capabilities is None:
        del payload["capabilities"]
    else:
        payload["capabilities"] = capabilities

    for path in (
        "/admin/routing/provider-route-models",
        "/admin/routing/provider-route-model-verifications",
    ):
        response = await client.post(path, json=payload, headers=AUTH)
        assert response.status_code == 422, response.text

    verify_mock.assert_not_awaited()
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    assert "deepseek-v4-flash" not in route_executor.routes


@pytest.mark.asyncio
async def test_runtime_model_capabilities_are_stored_once_in_a_fixed_order(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")

    response = await client.post(
        "/admin/routing/provider-route-models",
        json=_runtime_model_payload(
            capabilities={
                "context_length": 65536,
                "max_output_length": 65536,
                "input_modalities": ["image", "text", "image"],
                "supports_tools": False,
            }
        ),
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    declared = {
        "context_length": 65536,
        "max_output_length": 65536,
        "input_modalities": ["text", "image"],
        "supports_tools": False,
    }
    assert op_store.upsert_provider_route_candidate.await_args.args[12] == declared
    runtime_adapter = route_executor.routes["deepseek-v4-flash"].raw_adapters[0][0]
    assert runtime_adapter.config.input_modalities == ["text", "image"]
    assert runtime_adapter.config.supports_tools is False


@pytest.mark.asyncio
async def test_runtime_model_requests_follow_declared_capabilities(admin_client, monkeypatch):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    response = await client.post(
        "/admin/routing/provider-route-models",
        json=_runtime_model_payload(),
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    adapter = route_executor.routes["deepseek-v4-flash"].raw_adapters[0][0]
    sent = _capture_upstream_payloads(adapter, monkeypatch)

    # What a coding agent sends: tool definitions, a long output budget and,
    # on the chat surface, a screenshot.
    await adapter.chat_completion(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this picture?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                    },
                ],
            }
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="auto",
        max_tokens=32000,
    )
    await adapter.messages(
        {
            "model": "deepseek-v4-flash",
            "max_tokens": 32000,
            "tools": [
                {
                    "name": "read_file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "messages": [{"role": "user", "content": "Read README.md"}],
        },
        request_id="req-capabilities",
    )

    chat, anthropic = sent
    assert [tool["function"]["name"] for tool in chat["tools"]] == ["read_file"]
    assert chat["max_tokens"] == 32000
    assert [block["type"] for block in chat["messages"][0]["content"]] == ["text", "image_url"]
    assert [tool["function"]["name"] for tool in anthropic["tools"]] == ["read_file"]
    assert anthropic["max_tokens"] == 32000


@pytest.mark.asyncio
async def test_runtime_model_requests_hold_to_a_narrow_declaration(admin_client, monkeypatch):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    response = await client.post(
        "/admin/routing/provider-route-models",
        json=_runtime_model_payload(
            capabilities={
                "context_length": 32768,
                "max_output_length": 8192,
                "input_modalities": ["text"],
                "supports_tools": False,
            }
        ),
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    adapter = route_executor.routes["deepseek-v4-flash"].raw_adapters[0][0]
    sent = _capture_upstream_payloads(adapter, monkeypatch)

    await adapter.chat_completion(
        [{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        max_tokens=32000,
    )

    (payload,) = sent
    assert "tools" not in payload
    assert payload["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_runtime_route_added_to_a_declared_model_keeps_its_capabilities(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    create = await client.post(
        "/admin/routing/provider-route-models",
        json=_runtime_model_payload(strategy="routewise"),
        headers=AUTH,
    )
    assert create.status_code == 200, create.text
    added = await client.post(
        "/admin/routing/provider-route-candidates/deepseek-v4-flash",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "concurrency",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "concurrency_limit": 2,
            "weight": 1.0,
        },
        headers=AUTH,
    )
    assert added.status_code == 200, added.text

    # Editing the added route, as a concurrency edit does, must keep the
    # declaration the route was created with.
    response = await client.patch(
        "/admin/routing/provider-route-candidates/deepseek-v4-flash/"
        "deepseek-v4-flash:openrouter[parasail]-api",
        json={"concurrency_limit": 4},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    row = table.row("deepseek-v4-flash", "deepseek-v4-flash:openrouter[parasail]-api")
    assert json.loads(row["capabilities"]) == RUNTIME_CAPABILITIES
    assert row["concurrency_limit"] == 4
    added_adapter = route_executor.routes["deepseek-v4-flash"].raw_adapters[-1][0]
    assert added_adapter.config.route_metadata["declared_capabilities"] is True
    assert added_adapter.config.context_length == 131072
    assert added_adapter.config.supports_tools is True


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
            "capabilities": RUNTIME_CAPABILITIES,
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
@pytest.mark.parametrize("model_id", ["auto", "auto-balance", " auto-new "])
async def test_post_provider_route_model_refuses_an_auto_model_name(admin_client, model_id):
    """The auto models are served under these names; a model there would be hidden."""
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client

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
            "capabilities": RUNTIME_CAPABILITIES,
        },
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        f"Model id {model_id.strip()!r} is reserved for auto models"
    )
    assert model_id.strip() not in route_executor.routes
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    verify_mock.assert_not_awaited()


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
            "capabilities": RUNTIME_CAPABILITIES,
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
        RUNTIME_CAPABILITIES,
        "admin-token",
        deployment_type="auto",
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
            "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
        },
        headers=AUTH,
    )
    assert create_model.status_code == 200, create_model.text
    op_store.upsert_provider_route_candidate.reset_mock()

    response = await client.post(
        "/admin/routing/provider-route-candidates/deepseek-v4-flash",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
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
        RUNTIME_CAPABILITIES,
        "admin-token",
        deployment_type="auto",
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
            "capabilities": RUNTIME_CAPABILITIES,
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
                "admin-token",
            ),
            call(
                "model_required_role:deepseek-v4-flash",
                "free",
                "string",
                "admin-token",
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
            "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
                "capabilities": RUNTIME_CAPABILITIES,
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
                "capabilities": RUNTIME_CAPABILITIES,
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
                "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
        "capabilities": RUNTIME_CAPABILITIES,
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
                "capabilities": RUNTIME_CAPABILITIES,
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
    route_executor = FixedRouter()
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
                "capabilities": RUNTIME_CAPABILITIES,
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
        assert registry.get_router_name(runtime_model_id) == "fixed"
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
        # Publishing re-binds the live table on the new model's router. That
        # re-bind must not widen an unrelated RouteWise router past its own
        # model, or its background latency probe would start calling this
        # model's upstream too.
        assert set(existing_routewise.route_candidates) == {existing_model_id}
        assert registry.get_router_name(runtime_model_id) == "routewise"
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
    route_executor = FixedRouter()
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
            "capabilities": RUNTIME_CAPABILITIES,
        },
        headers=AUTH,
    )

    assert response.status_code == 409, response.text
    assert "quota-only pools" in response.json()["detail"]
    assert len(staged_routes) == 1
    assert staged_routes[0].published is False
    assert model_id not in route_executor.routes
    assert registry.get_router_name(model_id) == "fixed"
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
            "capabilities": RUNTIME_CAPABILITIES,
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
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    # The override below is live: the route already serves its endpoint.
    route_executor.routes["minimax-fast"].raw_adapters[0][
        0
    ].config.base_url = "https://api.featherless.ai/v1"
    base_key = "rc_base111111111111111111111111"
    numbered_key = "rc_numbered222222222222222222"
    monkeypatch.setenv("FEATHERLESS_API_KEY", base_key)
    monkeypatch.setenv("FEATHERLESS_API_KEY2", numbered_key)
    monkeypatch.delenv("FEATHERLESS_API_KEY3", raising=False)
    env_key_id = dynamic_keys.env_key_id(numbered_key)
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
            "capabilities": RUNTIME_CAPABILITIES,
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
                "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
        RUNTIME_CAPABILITIES,
        "admin-token",
        deployment_type="auto",
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
            "capabilities": RUNTIME_CAPABILITIES,
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
        RUNTIME_CAPABILITIES,
        "admin-token",
        deployment_type="auto",
    )
    verify_mock.assert_awaited_once()

    runtime_adapter = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert runtime_adapter.config.openrouter_pinned_provider == "parasail"
    assert runtime_adapter.config.openrouter_sort == "throughput"
    assert runtime_adapter.config.pricing == OPENROUTER_PARASAIL_PRICING
    assert runtime_adapter.config.route_metadata["pricing_provider"] == "parasail/fp8"


@pytest.mark.asyncio
async def test_post_provider_route_candidate_rejects_route_type_provider_mismatch(
    admin_client,
    monkeypatch,
):
    client, op_store, _route_executor, fake_routewise, verify_mock = admin_client
    # This deployment's Chutes plan is request-quota based.
    monkeypatch.setenv("PROVIDER_ROUTE_TYPES", "chutes=quota")
    get_settings.cache_clear()

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
    offload_route_resolver = MagicMock()
    offload_route_resolver.get_offload_policy.return_value = None
    client.app.state.services.offload_route_resolver = offload_route_resolver
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
            "capabilities": RUNTIME_CAPABILITIES,
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
            "model_offload_route:deepseek-v4-flash",
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
    # And a model later created under the same id must not inherit its offload route.
    offload_route_resolver.clear_model.assert_called_once_with("deepseek-v4-flash")
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
            "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
                "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
    # A row from before capabilities were declared restores exactly as it did.
    assert runtime_adapter.config.context_length == 8192
    assert runtime_adapter.config.max_output_length == 4096
    assert runtime_adapter.config.input_modalities == ["text"]
    assert runtime_adapter.config.supports_tools is False
    assert "declared_capabilities" not in runtime_adapter.config.route_metadata
    registry.prepare_router_strategy_change.assert_called_once_with("deepseek-v4-flash", "fixed")
    registry.commit_router_strategy_change.assert_called_once_with(change)
    registry.refresh_route_tables.assert_called_once_with()


# Two runtime routes of one model. The plain route sorts first, so restore
# builds the model from it and adds the pinned route next to it.
FIRST_ROUTE = ("deepseek-v4-flash:openrouter-api", "openrouter")
SECOND_ROUTE = ("deepseek-v4-flash:openrouter[parasail]-api", "openrouter[parasail]")
NARROW_CAPABILITIES = {
    "context_length": 32768,
    "max_output_length": 8192,
    "input_modalities": ["text"],
    "supports_tools": False,
}


def _stored_runtime_row(route: tuple[str, str], capabilities) -> dict:
    route_id, provider = route
    return {
        "model_id": "deepseek-v4-flash",
        "route_id": route_id,
        "route_type": "on_demand",
        "provider": provider,
        "openrouter_sort": None,
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_id": "db-openrouter",
        "provider_model_id": "deepseek/deepseek-v4-flash",
        "quota_limit": None,
        "concurrency_limit": None,
        "weight": 1.0,
        # asyncpg hands JSONB back as text on this pool.
        "pricing": json.dumps(RUNTIME_PRICING),
        "capabilities": json.dumps(capabilities) if capabilities is not None else None,
        "updated_at": NOW,
        "updated_by": "127.0.0.1",
    }


async def _restore_runtime_rows(admin_client, rows: list[dict]) -> dict[str, Any]:
    """Restore ``rows`` as a fixed runtime model; return its adapters by route id."""
    _client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.list_all_provider_route_candidates.return_value = rows
    op_store.list_settings.return_value = [
        {"key": "model_required_role:deepseek-v4-flash", "value": "admin"},
        {"key": "model_router_strategy:deepseek-v4-flash", "value": "fixed"},
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

    route = route_executor.routes.get("deepseek-v4-flash")
    if route is None:
        return {}
    return {
        adapter.config.route_metadata["route_id"]: adapter
        for adapter, _weight, _endpoint_id in route.raw_adapters
    }


def _assert_capabilities(adapter, capabilities: dict, *, declared: bool) -> None:
    assert adapter.config.context_length == capabilities["context_length"]
    assert adapter.config.max_output_length == capabilities["max_output_length"]
    assert adapter.config.input_modalities == capabilities["input_modalities"]
    assert adapter.config.supports_tools is capabilities["supports_tools"]
    # What the route writes back on its next save: its declaration, or nothing.
    assert provider_routes._declared_capabilities(adapter) == (capabilities if declared else None)


DEFAULT_CAPABILITIES = {
    "context_length": 8192,
    "max_output_length": 4096,
    "input_modalities": ["text"],
    "supports_tools": False,
}


@pytest.mark.asyncio
async def test_apply_persisted_provider_route_candidates_restores_declared_capabilities(
    admin_client,
):
    restored = await _restore_runtime_rows(
        admin_client,
        [
            _stored_runtime_row(FIRST_ROUTE, RUNTIME_CAPABILITIES),
            _stored_runtime_row(SECOND_ROUTE, RUNTIME_CAPABILITIES),
        ],
    )

    assert len(restored) == 2
    for adapter in restored.values():
        _assert_capabilities(adapter, RUNTIME_CAPABILITIES, declared=True)


@pytest.mark.asyncio
async def test_restore_gives_each_route_its_own_capabilities(admin_client):
    """A route restores its own row's values, not the first route's."""
    restored = await _restore_runtime_rows(
        admin_client,
        [
            _stored_runtime_row(FIRST_ROUTE, NARROW_CAPABILITIES),
            _stored_runtime_row(SECOND_ROUTE, RUNTIME_CAPABILITIES),
        ],
    )

    _assert_capabilities(restored[FIRST_ROUTE[0]], NARROW_CAPABILITIES, declared=True)
    _assert_capabilities(restored[SECOND_ROUTE[0]], RUNTIME_CAPABILITIES, declared=True)


@pytest.mark.asyncio
async def test_restore_keeps_a_declaration_behind_an_undeclared_first_route(admin_client):
    """A NULL row sorting first (added by an older build) does not erase the others.

    Before, the declared route copied the first route's defaults and lost its
    flag, so its next save wrote NULL over the declaration.
    """
    restored = await _restore_runtime_rows(
        admin_client,
        [
            _stored_runtime_row(FIRST_ROUTE, None),
            _stored_runtime_row(SECOND_ROUTE, RUNTIME_CAPABILITIES),
        ],
    )

    _assert_capabilities(restored[FIRST_ROUTE[0]], DEFAULT_CAPABILITIES, declared=False)
    _assert_capabilities(restored[SECOND_ROUTE[0]], RUNTIME_CAPABILITIES, declared=True)


@pytest.mark.asyncio
async def test_restore_does_not_declare_an_undeclared_route(admin_client):
    """A NULL row behind a declared route takes its values but writes nothing for them."""
    restored = await _restore_runtime_rows(
        admin_client,
        [
            _stored_runtime_row(FIRST_ROUTE, RUNTIME_CAPABILITIES),
            _stored_runtime_row(SECOND_ROUTE, None),
        ],
    )

    _assert_capabilities(restored[FIRST_ROUTE[0]], RUNTIME_CAPABILITIES, declared=True)
    second = restored[SECOND_ROUTE[0]]
    assert second.config.context_length == RUNTIME_CAPABILITIES["context_length"]
    assert provider_routes._declared_capabilities(second) is None


@pytest.mark.asyncio
async def test_restore_keeps_a_route_whose_stored_capabilities_no_longer_validate(
    admin_client, caplog
):
    """An invalid stored value restores the route undeclared instead of dropping it."""
    invalid = {**RUNTIME_CAPABILITIES, "input_modalities": ["text", "file"]}

    with caplog.at_level(logging.WARNING):
        restored = await _restore_runtime_rows(
            admin_client,
            [
                _stored_runtime_row(FIRST_ROUTE, invalid),
                _stored_runtime_row(SECOND_ROUTE, invalid),
            ],
        )

    assert len(restored) == 2
    for adapter in restored.values():
        _assert_capabilities(adapter, DEFAULT_CAPABILITIES, declared=False)
    assert "Ignoring invalid stored capabilities" in caplog.text


@pytest.mark.asyncio
async def test_candidate_snapshot_restore_decodes_jsonb_columns():
    op_store = MagicMock()
    op_store.upsert_provider_route_candidate = AsyncMock()

    await provider_routes._restore_provider_route_candidate_snapshot(
        op_store,
        model_id="deepseek-v4-flash",
        route_id="deepseek-v4-flash:openrouter-api",
        snapshot={
            "route_type": "on_demand",
            "provider": "openrouter",
            "openrouter_sort": None,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "deepseek/deepseek-v4-flash",
            "quota_limit": None,
            "concurrency_limit": None,
            "weight": 1.0,
            "pricing": json.dumps(RUNTIME_PRICING),
            "capabilities": json.dumps(RUNTIME_CAPABILITIES),
            "updated_by": "127.0.0.1",
        },
    )

    args = op_store.upsert_provider_route_candidate.await_args.args
    assert args[11] == RUNTIME_PRICING
    assert args[12] == RUNTIME_CAPABILITIES


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
        deployment_type=None,
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
                "capabilities": RUNTIME_CAPABILITIES,
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
            "capabilities": RUNTIME_CAPABILITIES,
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
        None,
        "seed-admin",
        deployment_type="auto",
        endpoint_id=None,
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
        deployment_type=None,
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
                "capabilities": RUNTIME_CAPABILITIES,
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
    assert audit.await_args.kwargs["action"] == "routing.provider_routes.candidate.create"


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


# ---------------------------------------------------------------------------
# null_cache_details_means_miss must not travel by clone
# ---------------------------------------------------------------------------
#
# Both admin paths build a new ModelConfig by cloning an existing route's --
# the first route for a new candidate, the edited route for an update -- and
# then overriding a fixed set of fields. The cache-report licence is a probe
# result about one endpoint ("this server was started with
# --enable-cache-report"), so inheriting it is exactly how an unprobed endpoint
# would start recording fabricated measured misses. On the four models that
# carry it in production the licensed sglang route is the *first* one, so it is
# the clone template for every fallback an admin adds.


def _license_first_route(route_executor, model_id: str = "minimax-fast"):
    """Stand in for a model whose first route is a licensed sglang node."""
    adapter = route_executor.routes[model_id].raw_adapters[0][0]
    adapter.config.null_cache_details_means_miss = True
    return adapter


@pytest.mark.asyncio
async def test_route_candidate_does_not_inherit_the_cache_report_licence(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _license_first_route(route_executor)
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
            "capabilities": RUNTIME_CAPABILITIES,
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

    assert response.status_code == 200, response.text
    added = route_executor.routes["minimax-fast"].raw_adapters[-1][0]
    assert added.config.endpoint_id == "minimax-fast:openrouter[parasail]-api"
    assert added.config.null_cache_details_means_miss is False


@pytest.mark.asyncio
async def test_route_update_drops_the_licence_when_the_endpoint_changes(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _license_first_route(route_executor)
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

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "quota_limit": 5000,
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    updated = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert updated.config.null_cache_details_means_miss is False


@pytest.mark.asyncio
async def test_route_update_keeps_the_licence_for_a_same_endpoint_key_rotation(admin_client):
    """Rotating the credential does not repoint the route, so the probe still holds."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _license_first_route(route_executor)
    op_store.get_provider_key_full.return_value = ("chutes", "chutes-db-key-1234567890")
    op_store.list_provider_keys.return_value = [
        ProviderKeyRow(
            id="db-chutes",
            provider="chutes",
            key_prefix="chutes...7890",
            label="rotated",
            status="active",
            created_at=NOW,
        )
    ]

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "chutes",
            "base_url": "https://llm.chutes.ai/v1",
            "api_key_id": "db-chutes",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5-TEE",
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    updated = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert updated.config.null_cache_details_means_miss is True


@pytest.mark.asyncio
async def test_route_update_keeps_the_licence_across_an_equivalent_url_spelling(admin_client):
    """A trailing slash is not a repointing.

    Adapters strip it before building the request URL, so the edit still points
    at the probed server; a raw string comparison would silently revoke the
    licence and send that endpoint's misses back to NULL.
    """
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _license_first_route(route_executor)
    op_store.get_provider_key_full.return_value = ("chutes", "chutes-db-key-1234567890")
    op_store.list_provider_keys.return_value = [
        ProviderKeyRow(
            id="db-chutes",
            provider="chutes",
            key_prefix="chutes...7890",
            label="rotated",
            status="active",
            created_at=NOW,
        )
    ]

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:chutes-api",
        json={
            "upstream_provider": "chutes",
            "base_url": "https://llm.chutes.ai/v1/",
            "api_key_id": "db-chutes",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5-TEE",
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    updated = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert updated.config.null_cache_details_means_miss is True


def test_canonical_base_url_folds_only_request_equivalent_spellings():
    canonical = provider_routes._canonical_base_url

    # Same upstream request -> same key.
    assert canonical("https://llm.chutes.ai/v1/") == canonical("https://llm.chutes.ai/v1")
    assert canonical("https://LLM.Chutes.AI/v1") == canonical("https://llm.chutes.ai/v1")
    assert canonical("https://llm.chutes.ai:443/v1") == canonical("https://llm.chutes.ai/v1")

    # Different upstream -> different key. A path or host change is a move.
    assert canonical("https://llm.chutes.ai/v2") != canonical("https://llm.chutes.ai/v1")
    assert canonical("https://other.chutes.ai/v1") != canonical("https://llm.chutes.ai/v1")
    assert canonical("https://llm.chutes.ai:8443/v1") != canonical("https://llm.chutes.ai/v1")


def test_canonical_base_url_keeps_rfc3986_params_in_the_path():
    """`urlparse` splits `;params` off the path; the adapter does not.

    `/v1;blue` and `/v1;green` build different upstream requests, so folding
    them together would carry an endpoint fact across two different endpoints --
    and a remote `endpoint_id` is only `<service>-api`, so it cannot catch it.
    """
    canonical = provider_routes._canonical_base_url

    assert canonical("https://llm.chutes.ai/v1;blue") != canonical("https://llm.chutes.ai/v1;green")
    assert canonical("https://llm.chutes.ai/v1;blue") != canonical("https://llm.chutes.ai/v1")


@pytest.mark.parametrize(
    "spelling",
    [
        None,
        "",
        "https://llm.chutes.ai/v1?",
        "https://llm.chutes.ai/v1#",
    ],
)
def test_canonical_base_url_refuses_what_it_cannot_resolve(spelling):
    """An unresolvable spelling yields None, which never compares equal.

    A bare `?`/`#` survives `_validate_base_url` (it only rejects a *non-empty*
    query or fragment) and the adapter carries it into the request path, while
    urlsplit drops it -- so the safe answer is to refuse rather than fold.
    """
    assert provider_routes._canonical_base_url(spelling) is None


def test_unresolvable_url_drops_the_licence_rather_than_carrying_it():
    cfg = {"endpoint_id": "m:chutes-api"}
    current = SimpleNamespace(
        endpoint_id="m:chutes-api",
        base_url="https://llm.chutes.ai/v1?",
        null_cache_details_means_miss=True,
    )

    provider_routes._carry_endpoint_facts_if_unmoved(
        cfg,
        current_adapter=SimpleNamespace(config=current),
        base_url="https://llm.chutes.ai/v1?",
    )

    assert "null_cache_details_means_miss" not in cfg


@pytest.mark.asyncio
async def test_deployment_type_edit_verify_legacy_update_and_reload(admin_client):
    client, op_store, executor, _routewise, verify_mock = admin_client
    route = executor.routes["minimax-fast"]
    adapter = route.raw_adapters[1][0]
    original_endpoint = adapter.config.endpoint_id
    adapter.config.base_url = "http://10.0.0.5:8000/v1"
    payload = {
        "upstream_provider": "featherless",
        "base_url": adapter.config.base_url,
        "provider_model_id": adapter.config.provider_model_id,
        "deployment_type": "self_hosted",
    }
    path = "minimax-fast/minimax-fast:featherless-api"
    response = await client.post(
        f"/admin/routing/provider-route-verifications/{path}", json=payload, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert verify_mock.await_args.args[0].adapter.config.deployment_type == "self_hosted"
    assert adapter.config.deployment_type == "auto"
    op_store.upsert_provider_route_config.assert_not_awaited()

    response = await client.put(
        f"/admin/routing/provider-routes/{path}", json=payload, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert response.json()["deployment_type"] == "self_hosted"
    assert response.json()["endpoint_id"] == original_endpoint
    assert op_store.upsert_provider_route_config.await_args.kwargs == {
        "deployment_type": "self_hosted"
    }
    updated = route.raw_adapters[1][0]
    assert updated.config.deployment_type == "self_hosted"
    assert updated.config.provider_type == "concurrency"
    assert updated.config.concurrency_pool == adapter.config.concurrency_pool

    columns = (
        "model_id",
        "route_id",
        "provider",
        "openrouter_sort",
        "base_url",
        "api_key_id",
        "provider_model_id",
        "quota_limit",
        "concurrency_limit",
        "updated_by",
    )

    def persisted_row():
        call = op_store.upsert_provider_route_config.await_args
        row = dict(zip(columns, call.args, strict=True))
        row.update(call.kwargs)
        return row

    op_store.list_provider_route_configs_for_model.return_value = [persisted_row()]

    # An older client changing credentials must not reset an explicit declaration.
    payload.pop("deployment_type")
    response = await client.put(
        f"/admin/routing/provider-routes/{path}", json=payload, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert response.json()["deployment_type"] == "self_hosted"
    persisted = persisted_row()
    assert persisted["deployment_type"] == "self_hosted"
    op_store.list_all_provider_route_configs.return_value = [persisted]
    # Simulate the freshly loaded YAML route before persisted overrides are applied.
    route.raw_adapters[1] = (adapter, route.raw_adapters[1][1], original_endpoint)
    await apply_persisted_provider_route_configs(client.app.state.services, op_store)
    assert route.raw_adapters[1][0].config.deployment_type == "self_hosted"

    # Explicit auto is a real reset, distinct from an omitted legacy field.
    payload["deployment_type"] = "auto"
    response = await client.put(
        f"/admin/routing/provider-routes/{path}", json=payload, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert response.json()["deployment_type"] == "auto"


@pytest.mark.asyncio
async def test_created_route_deployment_is_explicit_and_survives_reload(admin_client):
    client, op_store, executor, _routewise, verify_mock = admin_client
    route = executor.routes["minimax-fast"]
    route.raw_adapters[0][0].config.deployment_type = "self_hosted"
    original_entries = list(route.raw_adapters)
    payload = {
        "route_type": "on_demand",
        "upstream_provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "provider_model_id": "minimax/minimax-m2.5",
        "deployment_type": "provider_api",
        "weight": 1,
        "capabilities": RUNTIME_CAPABILITIES,
    }
    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast", json=payload, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert response.json()["deployment_type"] == "provider_api"
    assert verify_mock.await_args.args[0].adapter.config.deployment_type == "provider_api"
    args = op_store.upsert_provider_route_candidate.await_args.args
    columns = (
        "model_id",
        "route_id",
        "route_type",
        "provider",
        "openrouter_sort",
        "base_url",
        "api_key_id",
        "provider_model_id",
        "quota_limit",
        "concurrency_limit",
        "weight",
        "pricing",
        "capabilities",
        "updated_by",
    )
    persisted = dict(zip(columns, args, strict=True))
    persisted.update(op_store.upsert_provider_route_candidate.await_args.kwargs)
    op_store.list_all_provider_route_candidates.return_value = [persisted]
    route.raw_adapters[:] = original_entries
    await apply_persisted_provider_route_candidates(client.app.state.services, op_store)
    assert route.raw_adapters[-1][0].config.deployment_type == "provider_api"
    response = await client.get("/admin/routing/provider-routes/minimax-fast", headers=AUTH)
    restored = next(row for row in response.json()["routes"] if row["source"] == "runtime")
    assert restored["deployment_type"] == "provider_api"


@pytest.mark.asyncio
async def test_new_route_does_not_inherit_template_deployment_type(admin_client):
    client, _op_store, executor, _routewise, _verify_mock = admin_client
    executor.routes["minimax-fast"].raw_adapters[0][0].config.deployment_type = "self_hosted"
    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "provider_model_id": "minimax/minimax-m2.5",
            "weight": 1,
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["deployment_type"] == "auto"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["local", "", True, 1])
async def test_route_deployment_type_rejects_invalid_values(admin_client, value):
    client, _op_store, _executor, _routewise, verify_mock = admin_client
    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "featherless",
            "base_url": "https://api.featherless.ai/v1",
            "deployment_type": value,
        },
        headers=AUTH,
    )
    assert response.status_code == 422
    verify_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "provider"),
    [
        # A move still passes the address policy: cloud metadata is refused.
        ("http://169.254.169.254/v1", "featherless"),
        ("https://example.com:invalid/v1", "featherless"),
        ("https://[invalid/v1", "featherless"),
    ],
)
async def test_deployment_edit_still_validates_a_new_target(admin_client, base_url, provider):
    client, _store, executor, _routewise, verify_mock = admin_client
    executor.routes["minimax-fast"].raw_adapters[1][0].config.base_url = "http://10.0.0.5:8000/v1"
    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": provider,
            "base_url": base_url,
            "deployment_type": "self_hosted",
        },
        headers=AUTH,
    )
    assert response.status_code == 422
    verify_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "base_url", "declared_type", "expected_type", "persisted_type"),
    [
        ("featherless", "https://api.featherless.ai/v2", None, "auto", "auto"),
        ("openrouter", "https://openrouter.ai/api/v1", None, "auto", "auto"),
        ("openrouter", "https://openrouter.ai/api/v1", "self_hosted", "self_hosted", "self_hosted"),
        # Unmoved and undeclared: the row keeps following the models.yaml value.
        ("featherless", "api.featherless.ai/v1/", None, "self_hosted", None),
    ],
)
async def test_moving_route_does_not_inherit_old_endpoint_deployment_type(
    admin_client, provider, base_url, declared_type, expected_type, persisted_type
):
    client, op_store, executor, _routewise, verify_mock = admin_client
    route = executor.routes["minimax-fast"]
    original_entry = route.raw_adapters[1]
    original_entry[0].config.deployment_type = "self_hosted"
    payload = {
        "upstream_provider": provider,
        "base_url": base_url,
    }
    if provider == "openrouter":
        payload["openrouter_provider"] = "parasail"
    if declared_type is not None:
        payload["deployment_type"] = declared_type
    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json=payload,
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["deployment_type"] == expected_type
    assert verify_mock.await_args.args[0].adapter.config.deployment_type == expected_type
    assert op_store.upsert_provider_route_config.await_args.kwargs == {
        "deployment_type": persisted_type
    }

    # A pre-migration DB override also omitted ownership. Replay it over the
    # original YAML endpoint to check that restart follows the same rule.
    args = op_store.upsert_provider_route_config.await_args.args
    columns = (
        "model_id",
        "route_id",
        "provider",
        "openrouter_sort",
        "base_url",
        "api_key_id",
        "provider_model_id",
        "quota_limit",
        "concurrency_limit",
        "updated_by",
    )
    persisted = dict(zip(columns, args, strict=True))
    persisted["deployment_type"] = declared_type
    op_store.list_all_provider_route_configs.return_value = [persisted]
    route.raw_adapters[1] = original_entry
    await apply_persisted_provider_route_configs(client.app.state.services, op_store)
    assert route.raw_adapters[1][0].config.deployment_type == expected_type


def _persisted_route_config(op_store) -> dict:
    call = op_store.upsert_provider_route_config.await_args
    columns = (
        "model_id",
        "route_id",
        "provider",
        "openrouter_sort",
        "base_url",
        "api_key_id",
        "provider_model_id",
        "quota_limit",
        "concurrency_limit",
        "updated_by",
    )
    row = dict(zip(columns, call.args, strict=True))
    row.update(call.kwargs)
    return row


@pytest.mark.asyncio
async def test_undeclared_edit_keeps_following_yaml_deployment_type(admin_client):
    client, op_store, executor, _routewise, _verify_mock = admin_client
    route = executor.routes["minimax-fast"]
    yaml_entry = route.raw_adapters[1]
    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "featherless",
            "base_url": "https://api.featherless.ai/v1",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5",
            "concurrency_limit": 2,
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    row = _persisted_route_config(op_store)
    assert row["deployment_type"] is None

    # models.yaml later declares the engine; the saved limit must not pin "auto".
    yaml_entry[0].config.deployment_type = "self_hosted"
    route.raw_adapters[1] = yaml_entry
    op_store.list_all_provider_route_configs.return_value = [row]
    await apply_persisted_provider_route_configs(client.app.state.services, op_store)
    live = route.raw_adapters[1][0].config
    assert live.deployment_type == "self_hosted"
    assert live.concurrency["limit"] == 2


@pytest.mark.asyncio
async def test_route_list_reports_an_override_that_failed_to_apply(admin_client):
    client, op_store, executor, _routewise, _verify_mock = admin_client
    route = executor.routes["minimax-fast"]
    yaml_entry = route.raw_adapters[1]
    yaml_entry[0].config.base_url = "http://10.0.0.5:8000/v1"
    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "featherless",
            "base_url": "http://10.0.0.5:8000/v1",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5",
            "deployment_type": "self_hosted",
            "concurrency_limit": 4,
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["override_applied"] is True
    row = _persisted_route_config(op_store)

    # models.yaml moves the engine, and the saved target no longer passes the
    # address policy (here cloud metadata), so replay skips the row.
    yaml_entry[0].config.base_url = "http://10.0.0.6:8000/v1"
    row["base_url"] = "http://169.254.169.254/v1"
    route.raw_adapters[1] = yaml_entry
    op_store.list_all_provider_route_configs.return_value = [row]
    op_store.list_provider_route_configs_for_model.return_value = [row]
    await apply_persisted_provider_route_configs(client.app.state.services, op_store)

    response = await client.get("/admin/routing/provider-routes/minimax-fast", headers=AUTH)
    listed = next(
        item
        for item in response.json()["routes"]
        if item["route_id"] == "minimax-fast:featherless-api"
    )
    assert listed["source"] == "override"
    assert listed["override_applied"] is False
    assert listed["base_url"] == "http://10.0.0.6:8000/v1"
    assert listed["deployment_type"] == "auto"
    assert listed["concurrency_limit"] == 1


@pytest.mark.asyncio
async def test_undeclared_edit_does_not_keep_an_unapplied_rows_deployment_type(admin_client):
    client, op_store, executor, _routewise, _verify_mock = admin_client
    route = executor.routes["minimax-fast"]
    yaml_entry = route.raw_adapters[1]
    yaml_entry[0].config.base_url = "http://10.0.0.5:8000/v1"
    yaml_entry[0].config.deployment_type = "self_hosted"
    # A saved move to another engine that failed to apply at boot: models.yaml serves.
    unapplied = {
        "model_id": "minimax-fast",
        "route_id": "minimax-fast:featherless-api",
        "provider": "featherless",
        "openrouter_sort": None,
        "base_url": "http://10.0.0.9:8000/v1",
        "api_key_id": None,
        "provider_model_id": "MiniMaxAI/MiniMax-M2.5",
        "quota_limit": None,
        "concurrency_limit": None,
        "updated_by": "admin",
        "deployment_type": "auto",
    }
    op_store.list_provider_route_configs_for_model.return_value = [unapplied]

    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "featherless",
            "base_url": "http://10.0.0.5:8000/v1",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5",
            "concurrency_limit": 3,
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["deployment_type"] == "self_hosted"
    row = _persisted_route_config(op_store)
    assert row["deployment_type"] is None

    # Restart: the new row applies and must keep the live declaration.
    route.raw_adapters[1] = yaml_entry
    op_store.list_all_provider_route_configs.return_value = [row]
    await apply_persisted_provider_route_configs(client.app.state.services, op_store)
    assert route.raw_adapters[1][0].config.deployment_type == "self_hosted"


@pytest.mark.asyncio
async def test_pinned_openrouter_edit_keeps_its_deployment_type(admin_client):
    client, op_store, executor, _routewise, _verify_mock = admin_client
    route = executor.routes["minimax-fast"]
    pinned = route.raw_adapters[2][0]
    pinned.config.deployment_type = "provider_api"
    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:openrouter[deepinfra]-api",
        json={
            "upstream_provider": "openrouter",
            "openrouter_provider": "deepinfra",
            "base_url": "https://openrouter.ai/api/v1",
            "provider_model_id": "minimax/minimax-m2.5-turbo",
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["deployment_type"] == "provider_api"
    assert _persisted_route_config(op_store)["deployment_type"] is None
    live = route.raw_adapters[2][0].config
    assert live.deployment_type == "provider_api"
    assert live.endpoint_id == "minimax-fast:openrouter[deepinfra]-api"


@pytest.mark.asyncio
async def test_route_update_audit_records_deployment_type(admin_client):
    client, _op_store, _executor, _routewise, _verify_mock = admin_client
    response = await client.put(
        "/admin/routing/provider-routes/minimax-fast/minimax-fast:featherless-api",
        json={
            "upstream_provider": "featherless",
            "base_url": "https://api.featherless.ai/v1",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5",
            "deployment_type": "self_hosted",
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    audit = provider_routes.log_admin_action.await_args.kwargs
    action, details = audit["action"], audit["details"]
    assert action == "routing.provider_routes.update"
    assert details["old_deployment_type"] == "auto"
    assert details["deployment_type"] == "self_hosted"


# ---------------------------------------------------------------------------
# Disable, enable, delete and restore -- one set of operations for both sources
# ---------------------------------------------------------------------------


class _RouteStateTable:
    """Stateful stand-in for ``provider_route_states`` with a real resolver on top."""

    def __init__(self, client, op_store, route_executor) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        op_store.set_provider_route_state = AsyncMock(side_effect=self.set)
        op_store.list_all_provider_route_states = AsyncMock(side_effect=self.list_all)
        self.resolver = RouteStateResolver(op_store)
        client.app.state.services.route_state_resolver = self.resolver
        route_executor.route_state_resolver = self.resolver

    async def set(
        self,
        model_id,
        route_id,
        *,
        disabled,
        removed,
        updated_by,
        expected_disabled=None,
        expected_removed=None,
    ):
        if expected_disabled is not None:
            # Compare-and-set, as the store does: a route with no row has neither flag.
            row = self.rows.get((model_id, route_id))
            stored = (row["disabled"], row["removed"]) if row else (False, False)
            if stored != (expected_disabled, expected_removed):
                return False
        if not disabled and not removed:
            self.rows.pop((model_id, route_id), None)
            return True
        self.rows[(model_id, route_id)] = {
            "model_id": model_id,
            "route_id": route_id,
            "disabled": disabled,
            "removed": removed,
            "updated_by": updated_by,
        }
        return True

    async def list_all(self):
        return [dict(row) for _key, row in sorted(self.rows.items())]


FEATHERLESS = "minimax-fast:featherless-api"
CHUTES = "minimax-fast:chutes-api"
DEEPINFRA = "minimax-fast:openrouter[deepinfra]-api"


def _effective_weights(route_executor, model_id: str = "minimax-fast") -> dict[str, float]:
    for effective in route_executor.iter_effective_routes():
        if effective.canonical_model_id == model_id:
            return {
                adapter.config.route_metadata.get("route_id") or adapter.config.endpoint_id: weight
                for adapter, weight in effective.adapters
            }
    raise AssertionError(f"{model_id} is not routed")


async def _set_state(client, route_id: str, disabled: bool, model_id: str = "minimax-fast"):
    return await client.patch(
        f"/admin/routing/provider-route-states/{model_id}/{route_id}",
        json={"disabled": disabled},
        headers=AUTH,
    )


def _audited_actions() -> list[str]:
    return [call.kwargs["action"] for call in provider_routes.log_admin_action.await_args_list]


@pytest.mark.asyncio
async def test_disable_and_enable_a_config_route_keeps_its_weight(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    weights = _CandidateTable(op_store)
    resolver = _attach_weight_resolver(client, route_executor, weights)
    weights.weights[("minimax-fast", FEATHERLESS)] = 3.0
    resolver.set_override("minimax-fast", FEATHERLESS, 3.0)
    fake_routewise.refresh_route_table.reset_mock()

    disabled = await _set_state(client, FEATHERLESS, True)

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["disabled"] is True
    assert disabled.json()["source"] == "yaml"
    # Still configured, still weighted: only selection skips it.
    assert disabled.json()["effective_weight"] == 3.0
    assert states.rows[("minimax-fast", FEATHERLESS)]["disabled"] is True
    assert _effective_weights(route_executor)[FEATHERLESS] == 0.0
    assert weights.weights == {("minimax-fast", FEATHERLESS): 3.0}
    fake_routewise.refresh_route_table.assert_called_once_with()

    enabled = await _set_state(client, FEATHERLESS, False)

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["disabled"] is False
    assert states.rows == {}
    assert _effective_weights(route_executor)[FEATHERLESS] == 3.0
    assert _audited_actions()[-2:] == [
        "routing.provider_routes.disable",
        "routing.provider_routes.enable",
    ]


@pytest.mark.asyncio
async def test_disable_and_enable_a_runtime_route(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)

    disabled = await _set_state(client, MINIMAX_ROUTE_ID, True)
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["source"] == "runtime"
    assert disabled.json()["disabled"] is True
    assert _effective_weights(route_executor)[MINIMAX_ROUTE_ID] == 0.0

    # An edit keeps the route disabled: the state follows the route id.
    edited = await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)
    assert edited.status_code == 200, edited.text
    assert edited.json()["disabled"] is True
    assert _effective_weights(route_executor)[MINIMAX_ROUTE_ID] == 0.0

    enabled = await _set_state(client, MINIMAX_ROUTE_ID, False)
    assert enabled.status_code == 200, enabled.text
    assert _effective_weights(route_executor)[MINIMAX_ROUTE_ID] > 0
    assert states.rows == {}


@pytest.mark.asyncio
async def test_enable_clears_a_zero_weight_override_left_by_the_old_disable(admin_client):
    """Disable used to be a weight override of 0; Enable must not leave one behind."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    weights = _CandidateTable(op_store)
    resolver = _attach_weight_resolver(client, route_executor, weights)
    resolver.set_override("minimax-fast", FEATHERLESS, 0.0)
    assert _effective_weights(route_executor)[FEATHERLESS] == 0.0

    response = await _set_state(client, FEATHERLESS, False)

    assert response.status_code == 200, response.text
    op_store.delete_weight_override.assert_awaited_once_with("minimax-fast", FEATHERLESS)
    assert resolver.get_snapshot_for_model("minimax-fast") == {}
    assert _effective_weights(route_executor)[FEATHERLESS] == 1.0


@pytest.mark.asyncio
async def test_disabling_the_last_enabled_route_is_refused(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    assert (await _set_state(client, CHUTES, True)).status_code == 200
    assert (await _set_state(client, FEATHERLESS, True)).status_code == 200

    response = await _set_state(client, DEEPINFRA, True)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        f"route {DEEPINFRA!r} is the last enabled route of minimax-fast; enable or add "
        "another route before you disable it"
    )
    assert ("minimax-fast", DEEPINFRA) not in states.rows
    assert _effective_weights(route_executor)[DEEPINFRA] > 0


@pytest.mark.asyncio
async def test_a_failed_disable_leaves_the_route_enabled(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    fake_routewise.refresh_route_table.side_effect = [RuntimeError("rebuild failed"), None]

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await _set_state(client, FEATHERLESS, True)

    assert states.rows == {}
    assert not states.resolver.is_excluded("minimax-fast", FEATHERLESS)
    assert _effective_weights(route_executor)[FEATHERLESS] > 0
    provider_routes.log_admin_action.assert_not_awaited()


async def _remove(client, route_id: str, model_id: str = "minimax-fast"):
    return await client.post(
        f"/admin/routing/provider-route-removals/{model_id}/{route_id}", headers=AUTH
    )


async def _restore(client, route_id: str, model_id: str = "minimax-fast"):
    return await client.delete(
        f"/admin/routing/provider-route-removals/{model_id}/{route_id}", headers=AUTH
    )


def _live_route_ids(route_executor, model_id: str = "minimax-fast") -> list[str]:
    return [
        provider_routes._route_id_for_entry(adapter, endpoint_id)
        for adapter, _weight, endpoint_id in route_executor.routes[model_id].raw_adapters
    ]


@pytest.mark.asyncio
async def test_delete_and_restore_a_config_route_across_a_restart(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    weights = _CandidateTable(op_store)
    resolver = _attach_weight_resolver(client, route_executor, weights)
    resolver.set_override("minimax-fast", FEATHERLESS, 3.0)
    yaml_entries = list(route_executor.routes["minimax-fast"].raw_adapters)

    removed = await _remove(client, FEATHERLESS)

    assert removed.status_code == 200, removed.text
    rows = {row["route_id"]: row for row in removed.json()["routes"]}
    assert rows[FEATHERLESS]["removed"] is True
    assert rows[CHUTES]["removed"] is False
    assert _live_route_ids(route_executor) == [CHUTES, DEEPINFRA]
    assert states.rows[("minimax-fast", FEATHERLESS)]["removed"] is True
    # Its weight override is kept so a restore brings back what was there.
    assert resolver.get_snapshot_for_model("minimax-fast") == {FEATHERLESS: 3.0}
    assert _audited_actions()[-1] == "routing.provider_routes.remove"

    # Restart with the same models.yaml: the route stays deleted.
    restarted = FixedRouter()
    restarted.register_route("minimax-fast", yaml_entries_as_weights(yaml_entries))
    reloaded = RouteStateResolver(op_store)
    await reloaded.load_all()
    restarted.route_state_resolver = reloaded
    services = AppServices(
        router=restarted,
        model_router_registry=MagicMock(),
        operational_store=op_store,
        db_logger=MagicMock(),
        log_store=MagicMock(),
        route_state_resolver=reloaded,
    )
    provider_routes.apply_persisted_provider_route_removals(services)
    assert _live_route_ids(restarted) == [CHUTES, DEEPINFRA]

    restored = await _restore(client, FEATHERLESS)

    assert restored.status_code == 200, restored.text
    rows = {row["route_id"]: row for row in restored.json()["routes"]}
    assert rows[FEATHERLESS]["removed"] is False
    assert rows[FEATHERLESS]["effective_weight"] == 3.0
    # Back at its models.yaml position.
    assert _live_route_ids(route_executor) == [CHUTES, FEATHERLESS, DEEPINFRA]
    assert states.rows == {}
    assert _effective_weights(route_executor)[FEATHERLESS] == 3.0
    assert _audited_actions()[-1] == "routing.provider_routes.restore"
    assert fake_routewise.refresh_route_table.call_count >= 2


def yaml_entries_as_weights(entries):
    return [(adapter, weight) for adapter, weight, _endpoint_id in entries]


@pytest.mark.asyncio
async def test_restore_brings_back_the_routes_edit_override(admin_client):
    """Restoring undoes the delete only; Reset is what discards an edit."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    override = {
        "model_id": "minimax-fast",
        "route_id": CHUTES,
        "provider": "openrouter[parasail]",
        "openrouter_sort": None,
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_id": "db-openrouter",
        "provider_model_id": "minimax/minimax-m2.5",
        "quota_limit": 5000,
        "concurrency_limit": None,
        "deployment_type": None,
        "updated_at": NOW,
        "updated_by": "seed-admin",
    }
    op_store.list_provider_route_configs_for_model.return_value = [override]
    op_store.list_all_provider_route_configs.return_value = [override]
    await apply_persisted_provider_route_configs(client.app.state.services, op_store)
    assert route_executor.routes["minimax-fast"].raw_adapters[0][0].config.provider == "openrouter"

    assert (await _remove(client, CHUTES)).status_code == 200
    restored = await _restore(client, CHUTES)

    assert restored.status_code == 200, restored.text
    adapter = route_executor.routes["minimax-fast"].raw_adapters[0][0]
    assert adapter.config.route_metadata["route_id"] == CHUTES
    assert adapter.config.openrouter_pinned_provider == "parasail"
    op_store.delete_provider_route_config.assert_not_awaited()
    row = {r["route_id"]: r for r in restored.json()["routes"]}[CHUTES]
    assert row["source"] == "override"
    assert row["override_applied"] is True


@pytest.mark.asyncio
async def test_a_disabled_config_route_comes_back_disabled(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    assert (await _set_state(client, FEATHERLESS, True)).status_code == 200
    assert (await _remove(client, FEATHERLESS)).status_code == 200

    # A deleted route is restored, not edited or toggled.
    toggled = await _set_state(client, FEATHERLESS, False)
    assert toggled.status_code == 409, toggled.text
    assert toggled.json()["detail"] == (
        f"route {FEATHERLESS!r} is deleted from this deployment; restore it first"
    )

    restored = await _restore(client, FEATHERLESS)
    row = {r["route_id"]: r for r in restored.json()["routes"]}[FEATHERLESS]
    assert row["disabled"] is True
    assert _effective_weights(route_executor)[FEATHERLESS] == 0.0


@pytest.mark.asyncio
async def test_deleting_a_config_offload_route_is_refused(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    client.app.state.services.offload_route_resolver = SimpleNamespace(
        get_offload_policy=lambda model_id: SimpleNamespace(route_id=FEATHERLESS)
    )

    response = await _remove(client, FEATHERLESS)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        f"route {FEATHERLESS!r} is the model's offload route; clear the offload route "
        "before deleting it"
    )
    assert states.rows == {}
    assert FEATHERLESS in _live_route_ids(route_executor)


@pytest.mark.asyncio
async def test_deleting_the_last_enabled_config_route_is_refused(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    assert (await _set_state(client, CHUTES, True)).status_code == 200
    assert (await _set_state(client, FEATHERLESS, True)).status_code == 200

    response = await _remove(client, DEEPINFRA)

    assert response.status_code == 409, response.text
    assert "is the last enabled route of minimax-fast" in response.json()["detail"]
    assert DEEPINFRA in _live_route_ids(route_executor)
    # A disabled route can still be deleted: it serves nothing either way.
    assert (await _remove(client, CHUTES)).status_code == 200


@pytest.mark.asyncio
async def test_a_failed_config_route_delete_keeps_the_route(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    fake_routewise.refresh_route_table.side_effect = [RuntimeError("rebuild failed"), None]
    before = list(route_executor.routes["minimax-fast"].raw_adapters)

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await _remove(client, FEATHERLESS)

    assert route_executor.routes["minimax-fast"].raw_adapters == before
    assert states.rows == {}
    assert not states.resolver.is_excluded("minimax-fast", FEATHERLESS)


@pytest.mark.asyncio
async def test_a_runtime_route_is_deleted_as_a_candidate_not_marked_removed(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)

    marked = await _remove(client, MINIMAX_ROUTE_ID)
    assert marked.status_code == 400, marked.text
    assert marked.json()["detail"] == "Runtime-added provider routes must be deleted as candidates"

    deleted = await client.delete(
        f"/admin/routing/provider-route-candidates/minimax-fast/{MINIMAX_ROUTE_ID}",
        headers=AUTH,
    )
    assert deleted.status_code == 200, deleted.text
    assert ("minimax-fast", MINIMAX_ROUTE_ID) not in table.rows
    assert MINIMAX_ROUTE_ID not in _live_route_ids(route_executor)


@pytest.mark.asyncio
async def test_deleting_the_last_enabled_runtime_route_is_refused(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    for route_id in (CHUTES, FEATHERLESS, DEEPINFRA):
        assert (await _set_state(client, route_id, True)).status_code == 200

    response = await client.delete(
        f"/admin/routing/provider-route-candidates/minimax-fast/{MINIMAX_ROUTE_ID}",
        headers=AUTH,
    )

    assert response.status_code == 409, response.text
    assert "is the last enabled route of minimax-fast" in response.json()["detail"]
    assert MINIMAX_ROUTE_ID in _live_route_ids(route_executor)


@pytest.mark.asyncio
async def test_a_deleted_config_routes_ids_stay_reserved(admin_client):
    """A route added while a config route is deleted cannot take ids its restore needs."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    _CandidateTable(op_store)
    assert (await _remove(client, FEATHERLESS)).status_code == 200
    op_store.list_provider_keys_full.return_value = ["featherless-db-key-1234567890"]

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "concurrency",
            "upstream_provider": "featherless",
            "base_url": "https://api.featherless.ai/v1",
            "provider_model_id": "MiniMaxAI/MiniMax-M2.5",
            "concurrency_limit": 1,
            "weight": 1.0,
        },
        headers=AUTH,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "provider route already exists"


@pytest.mark.asyncio
async def test_restoring_a_route_that_is_not_deleted_is_refused(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)

    response = await _restore(client, FEATHERLESS)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"route {FEATHERLESS!r} is not deleted"


@pytest.mark.asyncio
async def test_route_state_endpoints_require_admin(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    bad = {"Authorization": "Bearer not-the-admin-token"}

    responses = [
        await client.patch(
            f"/admin/routing/provider-route-states/minimax-fast/{FEATHERLESS}",
            json={"disabled": True},
            headers=bad,
        ),
        await client.post(
            f"/admin/routing/provider-route-removals/minimax-fast/{FEATHERLESS}", headers=bad
        ),
        await client.delete(
            f"/admin/routing/provider-route-removals/minimax-fast/{FEATHERLESS}", headers=bad
        ),
    ]

    assert all(response.status_code in {401, 403} for response in responses)
    assert states.rows == {}


@pytest.mark.asyncio
async def test_config_route_edit_refuses_an_endpoint_another_route_holds(admin_client):
    """Moving a config route onto a runtime route's endpoint would merge their state."""
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    verify_mock.reset_mock()
    before = list(route_executor.routes["minimax-fast"].raw_adapters)

    response = await client.put(
        f"/admin/routing/provider-routes/minimax-fast/{CHUTES}",
        json={
            "upstream_provider": "minimax",
            "base_url": "https://api.minimax.io/v1",
            "provider_model_id": "MiniMax-M2.5",
        },
        headers=AUTH,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        f"another route of this model already uses endpoint id {MINIMAX_ROUTE_ID!r}"
    )
    verify_mock.assert_not_awaited()
    op_store.upsert_provider_route_config.assert_not_awaited()
    assert route_executor.routes["minimax-fast"].raw_adapters == before


# -- Editing an existing route's capabilities -------------------------------

YAML_ROUTE_PATH = "minimax-fast/minimax-fast:featherless-api"


def _route_adapter(route_executor, route_id: str):
    for adapter, _weight, endpoint_id in route_executor.routes["minimax-fast"].raw_adapters:
        if provider_routes._route_id_for_entry(adapter, endpoint_id) == route_id:
            return adapter
    raise AssertionError(f"no route {route_id}")


@pytest.mark.asyncio
async def test_put_route_capabilities_overrides_a_yaml_route(admin_client, monkeypatch):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client

    response = await client.put(
        f"/admin/routing/provider-route-capabilities/{YAML_ROUTE_PATH}",
        json=RUNTIME_CAPABILITIES,
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["capabilities"] == RUNTIME_CAPABILITIES
    assert body["capabilities_baseline"] == DEFAULT_CAPABILITIES
    assert body["capabilities_overridden"] is True
    op_store.upsert_route_capability_override.assert_awaited_once_with(
        "minimax-fast", "minimax-fast:featherless-api", RUNTIME_CAPABILITIES, "admin-token"
    )
    # Only the edited route changes; its siblings keep their own values.
    _assert_capabilities_values(
        _route_adapter(route_executor, "minimax-fast:chutes-api"), DEFAULT_CAPABILITIES
    )
    audit = provider_routes.log_admin_action
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "routing.provider_routes.capabilities.update"
    assert audit.await_args.kwargs["details"]["old_capabilities"] == DEFAULT_CAPABILITIES
    assert audit.await_args.kwargs["details"]["capabilities"] == RUNTIME_CAPABILITIES

    # The next request through the route is served with the new values.
    adapter = _route_adapter(route_executor, "minimax-fast:featherless-api")
    sent = _capture_upstream_payloads(adapter, monkeypatch)
    await adapter.chat_completion(
        [{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
        max_tokens=32000,
    )
    (payload,) = sent
    assert payload["tools"]
    assert payload["max_tokens"] == 32000


@pytest.mark.asyncio
async def test_delete_route_capabilities_returns_the_route_to_its_baseline(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    path = f"/admin/routing/provider-route-capabilities/{YAML_ROUTE_PATH}"
    await client.put(path, json=RUNTIME_CAPABILITIES, headers=AUTH)

    response = await client.delete(path, headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["capabilities"] == DEFAULT_CAPABILITIES
    assert body["capabilities_overridden"] is False
    assert op_store.capability_rows == {}
    adapter = _route_adapter(route_executor, "minimax-fast:featherless-api")
    _assert_capabilities_values(adapter, DEFAULT_CAPABILITIES)
    assert provider_routes._CAPABILITY_BASELINE not in adapter.config.route_metadata


@pytest.mark.asyncio
async def test_put_route_capabilities_keeps_audio_and_video(admin_client):
    """Raising a route's max output keeps the audio and video input it declares."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    adapter = _route_adapter(route_executor, "minimax-fast:featherless-api")
    # As a models.yaml route can declare it.
    adapter.config.input_modalities = ["text", "image", "audio", "video"]
    # What the console sends: the route's own modalities, with only the output raised.
    edited = {**DEFAULT_CAPABILITIES, "max_output_length": 8192}
    edited["input_modalities"] = ["text", "image", "audio", "video"]

    response = await client.put(
        f"/admin/routing/provider-route-capabilities/{YAML_ROUTE_PATH}",
        json=edited,
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.json()["capabilities"] == edited
    _assert_capabilities_values(adapter, edited)
    stored = op_store.capability_rows[("minimax-fast", "minimax-fast:featherless-api")]
    assert json.loads(stored["capabilities"]) == edited
    # Audio and video requests are still routed to it.
    assert adapter_supports_modalities(adapter, frozenset({"audio", "video"}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [
        {**RUNTIME_CAPABILITIES, "context_length": 8192, "max_output_length": 16384},
        {**RUNTIME_CAPABILITIES, "input_modalities": ["image"]},
        {**RUNTIME_CAPABILITIES, "input_modalities": ["text", "file"]},
        {**RUNTIME_CAPABILITIES, "supports_tools": "yes"},
        {key: value for key, value in RUNTIME_CAPABILITIES.items() if key != "context_length"},
    ],
    ids=["output-over-context", "no-text", "unknown-modality", "string-tools", "missing-field"],
)
async def test_put_route_capabilities_rejects_invalid_values(admin_client, capabilities):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client

    response = await client.put(
        f"/admin/routing/provider-route-capabilities/{YAML_ROUTE_PATH}",
        json=capabilities,
        headers=AUTH,
    )

    assert response.status_code == 422
    op_store.upsert_route_capability_override.assert_not_awaited()
    _assert_capabilities_values(
        _route_adapter(route_executor, "minimax-fast:featherless-api"), DEFAULT_CAPABILITIES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "status"),
    [("no-such-model/no-such-model:x-api", 404), ("minimax-fast/minimax-fast:nope-api", 400)],
)
async def test_put_route_capabilities_rejects_unknown_routes(admin_client, path, status):
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client

    response = await client.put(
        f"/admin/routing/provider-route-capabilities/{path}",
        json=RUNTIME_CAPABILITIES,
        headers=AUTH,
    )

    assert response.status_code == status, response.text
    op_store.upsert_route_capability_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_capability_override_survives_a_target_edit_and_its_reset(admin_client):
    """Rebuilding the route (a new target, then back to models.yaml) keeps the override."""
    client, _op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    await client.put(
        f"/admin/routing/provider-route-capabilities/{YAML_ROUTE_PATH}",
        json=NARROW_CAPABILITIES,
        headers=AUTH,
    )

    edited = await client.put(
        f"/admin/routing/provider-routes/{YAML_ROUTE_PATH}",
        json={
            "upstream_provider": "featherless",
            "base_url": "https://api.featherless.ai/v2",
        },
        headers=AUTH,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["capabilities"] == NARROW_CAPABILITIES
    _assert_capabilities_values(
        _route_adapter(route_executor, "minimax-fast:featherless-api"), NARROW_CAPABILITIES
    )

    # Changed while the edited target serves: the models.yaml adapter that the
    # reset reinstalls still holds the earlier values until it is reconciled.
    await client.put(
        f"/admin/routing/provider-route-capabilities/{YAML_ROUTE_PATH}",
        json=RUNTIME_CAPABILITIES,
        headers=AUTH,
    )
    reset = await client.delete(f"/admin/routing/provider-routes/{YAML_ROUTE_PATH}", headers=AUTH)
    assert reset.status_code == 200, reset.text
    assert reset.json()["capabilities"] == RUNTIME_CAPABILITIES
    _assert_capabilities_values(
        _route_adapter(route_executor, "minimax-fast:featherless-api"), RUNTIME_CAPABILITIES
    )

    # Clearing the override after the rebuilds still finds the models.yaml values.
    cleared = await client.delete(
        f"/admin/routing/provider-route-capabilities/{YAML_ROUTE_PATH}", headers=AUTH
    )
    assert cleared.json()["capabilities"] == DEFAULT_CAPABILITIES


@pytest.mark.asyncio
async def test_boot_sync_applies_stored_overrides_and_skips_invalid_rows(admin_client, caplog):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.capability_rows[("minimax-fast", "minimax-fast:featherless-api")] = {
        "model_id": "minimax-fast",
        "route_id": "minimax-fast:featherless-api",
        "capabilities": json.dumps(RUNTIME_CAPABILITIES),
    }
    op_store.capability_rows[("minimax-fast", "minimax-fast:chutes-api")] = {
        "model_id": "minimax-fast",
        "route_id": "minimax-fast:chutes-api",
        "capabilities": json.dumps({**RUNTIME_CAPABILITIES, "input_modalities": ["audio"]}),
    }
    # A row for a model this deployment no longer serves is ignored.
    op_store.capability_rows[("retired-model", "retired-model:x-api")] = {
        "model_id": "retired-model",
        "route_id": "retired-model:x-api",
        "capabilities": json.dumps(RUNTIME_CAPABILITIES),
    }

    with caplog.at_level(logging.WARNING):
        await provider_routes.sync_route_capability_overrides(client.app.state.services, op_store)

    _assert_capabilities_values(
        _route_adapter(route_executor, "minimax-fast:featherless-api"), RUNTIME_CAPABILITIES
    )
    _assert_capabilities_values(
        _route_adapter(route_executor, "minimax-fast:chutes-api"), DEFAULT_CAPABILITIES
    )
    assert "Ignoring invalid capability override" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_id", "orphaned"),
    [
        (
            None,
            [
                ("minimax-fast", "minimax-fast:10.0.0.5-api"),
                ("retired-model", "retired-model:x-api"),
            ],
        ),
        ("minimax-fast", [("minimax-fast", "minimax-fast:10.0.0.5-api")]),
    ],
    ids=["boot", "one-model"],
)
async def test_sync_warns_about_overrides_that_name_no_served_route(
    admin_client, caplog, model_id, orphaned
):
    """A row nothing applies is reported: the console lists routes, not rows."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    for row_model_id, route_id in (
        ("minimax-fast", "minimax-fast:featherless-api"),
        # The id a route had before a models.yaml URL change minted a new one.
        ("minimax-fast", "minimax-fast:10.0.0.5-api"),
        ("retired-model", "retired-model:x-api"),
    ):
        op_store.capability_rows[(row_model_id, route_id)] = {
            "model_id": row_model_id,
            "route_id": route_id,
            "capabilities": json.dumps(RUNTIME_CAPABILITIES),
        }

    with caplog.at_level(logging.WARNING):
        await provider_routes.sync_route_capability_overrides(
            client.app.state.services, op_store, model_id
        )

    warned = [
        (record.levelno, record.model_id, record.route_id)
        for record in caplog.records
        if getattr(record, "event", None) == "route_capability_override_orphaned"
    ]
    assert warned == [(logging.WARNING, *row) for row in orphaned]
    _assert_capabilities_values(
        _route_adapter(route_executor, "minimax-fast:featherless-api"), RUNTIME_CAPABILITIES
    )


@pytest.mark.asyncio
async def test_post_provider_route_candidate_requires_its_own_capabilities(admin_client):
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    payload = {
        "route_type": "on_demand",
        "upstream_provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "provider_model_id": "minimax/minimax-m2.5",
        "weight": 1,
    }

    missing = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast", json=payload, headers=AUTH
    )
    assert missing.status_code == 422
    verify_mock.assert_not_awaited()

    added = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={**payload, "capabilities": NARROW_CAPABILITIES},
        headers=AUTH,
    )
    assert added.status_code == 200, added.text
    route_id = added.json()["route_id"]
    # The new route serves with what it declared, not with the first route's values.
    _assert_capabilities_values(_route_adapter(route_executor, route_id), NARROW_CAPABILITIES)
    _assert_capabilities_values(
        _route_adapter(route_executor, "minimax-fast:chutes-api"), DEFAULT_CAPABILITIES
    )
    assert op_store.upsert_provider_route_candidate.await_args.args[12] == NARROW_CAPABILITIES


@pytest.mark.asyncio
async def test_runtime_route_keeps_its_declaration_under_an_override(admin_client):
    """A save of an overridden runtime route keeps its declaration, not the override."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    added = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "concurrency",
            "upstream_provider": "openrouter",
            "openrouter_provider": "parasail",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_id": "db-openrouter",
            "provider_model_id": "minimax/minimax-m2.5",
            "concurrency_limit": 2,
            "capabilities": NARROW_CAPABILITIES,
        },
        headers=AUTH,
    )
    assert added.status_code == 200, added.text
    route_id = added.json()["route_id"]
    await client.put(
        f"/admin/routing/provider-route-capabilities/minimax-fast/{route_id}",
        json=RUNTIME_CAPABILITIES,
        headers=AUTH,
    )

    edited = await client.patch(
        f"/admin/routing/provider-route-candidates/minimax-fast/{route_id}",
        json={"concurrency_limit": 3},
        headers=AUTH,
    )

    assert edited.status_code == 200, edited.text
    # The edit updates the row in place and leaves its declaration as stored.
    op_store.update_provider_route_candidate.assert_awaited_once()
    assert json.loads(table.row("minimax-fast", route_id)["capabilities"]) == NARROW_CAPABILITIES
    adapter = _route_adapter(route_executor, route_id)
    assert provider_routes._declared_capabilities(adapter) == NARROW_CAPABILITIES
    _assert_capabilities_values(adapter, RUNTIME_CAPABILITIES)


@pytest.mark.asyncio
async def test_deleting_a_runtime_route_drops_its_capability_override(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    added = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "route_type": "on_demand",
            "upstream_provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "provider_model_id": "minimax/minimax-m2.5",
            "capabilities": NARROW_CAPABILITIES,
        },
        headers=AUTH,
    )
    route_id = added.json()["route_id"]
    await client.put(
        f"/admin/routing/provider-route-capabilities/minimax-fast/{route_id}",
        json=RUNTIME_CAPABILITIES,
        headers=AUTH,
    )
    assert ("minimax-fast", route_id) in op_store.capability_rows

    deleted = await client.delete(
        f"/admin/routing/provider-route-candidates/minimax-fast/{route_id}", headers=AUTH
    )

    assert deleted.status_code == 200, deleted.text
    assert ("minimax-fast", route_id) not in op_store.capability_rows
    overrides = getattr(
        route_executor.routes["minimax-fast"], provider_routes.CAPABILITY_OVERRIDES_ATTR
    )
    assert route_id not in overrides


OPENROUTER_ROUTE_PAYLOAD = {
    "route_type": "on_demand",
    "upstream_provider": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "provider_model_id": "minimax/minimax-m2.5",
    "capabilities": NARROW_CAPABILITIES,
}


@pytest.mark.asyncio
async def test_adding_a_route_is_refused_while_its_id_keeps_an_override(admin_client):
    """Added past a row it could not clear, the route would serve with the row's values."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.delete_route_capability_override.side_effect = ConnectionError("database down")

    response = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json=OPENROUTER_ROUTE_PAYLOAD,
        headers=AUTH,
    )

    assert response.status_code == 503, response.text
    assert "the route was not added" in response.text
    op_store.upsert_provider_route_candidate.assert_not_awaited()
    assert len(route_executor.routes["minimax-fast"].raw_adapters) == 3


@pytest.mark.asyncio
async def test_deleting_a_route_stands_when_its_override_cannot_be_dropped(admin_client, caplog):
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client
    added = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json=OPENROUTER_ROUTE_PAYLOAD,
        headers=AUTH,
    )
    route_id = added.json()["route_id"]
    await client.put(
        f"/admin/routing/provider-route-capabilities/minimax-fast/{route_id}",
        json=RUNTIME_CAPABILITIES,
        headers=AUTH,
    )
    op_store.delete_route_capability_override.side_effect = ConnectionError("database down")

    with caplog.at_level(logging.ERROR):
        deleted = await client.delete(
            f"/admin/routing/provider-route-candidates/minimax-fast/{route_id}", headers=AUTH
        )

    # The route is gone; its leftover row is logged loudly, by id, for an operator.
    assert deleted.status_code == 200, deleted.text
    assert route_id not in [route["route_id"] for route in deleted.json()["routes"]]
    assert ("minimax-fast", route_id) in op_store.capability_rows
    assert [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and route_id in record.getMessage()
    ]


@pytest.mark.asyncio
async def test_creating_a_model_clears_capability_overrides_left_under_its_name(admin_client):
    client, op_store, _route_executor, _fake_routewise, _verify_mock = admin_client
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    op_store.capability_rows[("deepseek-v4-flash", "deepseek-v4-flash:openrouter-api")] = {
        "model_id": "deepseek-v4-flash",
        "route_id": "deepseek-v4-flash:openrouter-api",
        "capabilities": json.dumps(NARROW_CAPABILITIES),
    }

    response = await client.post(
        "/admin/routing/provider-route-models", json=_runtime_model_payload(), headers=AUTH
    )

    assert response.status_code == 200, response.text
    assert op_store.capability_rows == {}
    assert response.json()["capabilities"] == RUNTIME_CAPABILITIES


def _assert_capabilities_values(adapter, capabilities: dict) -> None:
    assert provider_routes._config_capabilities(adapter.config) == capabilities


# -- Capability overrides across route edits, deletes and states -------------
#
# An override is keyed by route id, which every edit keeps: it follows its route
# the way a weight override follows the route's endpoint.

_RUNTIME_MODEL_FIRST_ROUTE_PARASAIL = {
    "upstream_provider": "openrouter",
    "openrouter_provider": "parasail",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_id": "db-openrouter",
    "provider_model_id": "deepseek/deepseek-v4-pro",
}


async def _overridable_runtime_route(client, op_store, case: str) -> tuple[str, str, dict, str]:
    """Add a runtime route that declares RUNTIME_CAPABILITIES; return how to retarget it."""
    if case == "added-route":
        await _add_minimax_runtime_route(client, op_store)
        return "minimax-fast", MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET, PARASAIL_ENDPOINT_ID
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    created = await client.post(
        "/admin/routing/provider-route-models", json=_runtime_model_payload(), headers=AUTH
    )
    assert created.status_code == 200, created.text
    return (
        "deepseek-v4-flash",
        created.json()["route_id"],
        _RUNTIME_MODEL_FIRST_ROUTE_PARASAIL,
        "deepseek-v4-flash:openrouter[parasail]-api",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["added-route", "model-first-route"])
async def test_runtime_route_edit_keeps_its_capability_override_and_declaration(admin_client, case):
    """A retarget moves the weight override; the capability override stays on the route id."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    weights = _attach_weight_resolver(client, route_executor, table)
    model_id, route_id, retarget, new_endpoint_id = await _overridable_runtime_route(
        client, op_store, case
    )
    table.weights[(model_id, route_id)] = 3.0
    weights.set_override(model_id, route_id, 3.0)
    path = f"/admin/routing/provider-route-capabilities/{model_id}/{route_id}"
    assert (await client.put(path, json=NARROW_CAPABILITIES, headers=AUTH)).status_code == 200

    edited = await _patch_candidate(client, route_id, retarget, model_id=model_id)

    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["endpoint_id"] == new_endpoint_id
    assert body["capabilities"] == NARROW_CAPABILITIES
    assert body["capabilities_baseline"] == RUNTIME_CAPABILITIES
    assert body["capabilities_overridden"] is True
    assert table.weights == {(model_id, new_endpoint_id): 3.0}
    assert set(op_store.capability_rows) == {(model_id, route_id)}
    # The row and the rebuilt adapter keep the declaration, never the override.
    assert json.loads(table.row(model_id, route_id)["capabilities"]) == RUNTIME_CAPABILITIES
    adapter = _live_entry(route_executor, route_id, model_id=model_id)[1]
    assert provider_routes._declared_capabilities(adapter) == RUNTIME_CAPABILITIES
    _assert_capabilities_values(adapter, NARROW_CAPABILITIES)

    # Resetting it afterwards returns the moved route to its declaration.
    reset = await client.delete(path, headers=AUTH)
    assert reset.status_code == 200, reset.text
    assert reset.json()["capabilities"] == RUNTIME_CAPABILITIES
    assert reset.json()["capabilities_overridden"] is False


def _seed_chutes_edit(op_store) -> None:
    """Store an edit moving the CHUTES config route to OpenRouter pinned to Parasail."""
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    override = {
        "model_id": "minimax-fast",
        "route_id": CHUTES,
        "provider": "openrouter[parasail]",
        "openrouter_sort": None,
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_id": "db-openrouter",
        "provider_model_id": "minimax/minimax-m2.5",
        "quota_limit": 5000,
        "concurrency_limit": None,
        "deployment_type": None,
        "updated_at": NOW,
        "updated_by": "seed-admin",
    }
    op_store.list_provider_route_configs_for_model.return_value = [override]
    op_store.list_all_provider_route_configs.return_value = [override]


def _orphan_warnings(caplog) -> list[tuple[str, str]]:
    return [
        (record.model_id, record.route_id)
        for record in caplog.records
        if getattr(record, "event", None) == "route_capability_override_orphaned"
    ]


@pytest.mark.asyncio
async def test_a_deleted_config_route_keeps_its_capability_override_for_its_restore(
    admin_client, caplog
):
    """Restore brings the route back as it was deleted: its edit and its capabilities."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    _seed_chutes_edit(op_store)
    await apply_persisted_provider_route_configs(client.app.state.services, op_store)
    chutes_path = f"/admin/routing/provider-route-capabilities/minimax-fast/{CHUTES}"
    assert (
        await client.put(chutes_path, json=NARROW_CAPABILITIES, headers=AUTH)
    ).status_code == 200

    removed = await _remove(client, CHUTES)

    assert removed.status_code == 200, removed.text
    assert ("minimax-fast", CHUTES) in op_store.capability_rows
    row = {r["route_id"]: r for r in removed.json()["routes"]}[CHUTES]
    assert row["removed"] is True
    # Listed with the values its restore brings back.
    assert row["capabilities"] == NARROW_CAPABILITIES
    assert row["capabilities_overridden"] is True

    # A sibling's edit re-reads the model's overrides; the deleted route's row is
    # kept for its restore, not reported as naming no route.
    with caplog.at_level(logging.WARNING):
        sibling = await client.put(
            f"/admin/routing/provider-route-capabilities/minimax-fast/{FEATHERLESS}",
            json=RUNTIME_CAPABILITIES,
            headers=AUTH,
        )
        await provider_routes.sync_route_capability_overrides(client.app.state.services, op_store)
    assert sibling.status_code == 200, sibling.text
    assert _orphan_warnings(caplog) == []

    # A deleted route is restored, not edited.
    refused = await client.put(chutes_path, json=RUNTIME_CAPABILITIES, headers=AUTH)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == (
        f"route {CHUTES!r} is deleted from this deployment; restore it first"
    )
    assert json.loads(op_store.capability_rows[("minimax-fast", CHUTES)]["capabilities"]) == (
        NARROW_CAPABILITIES
    )

    restored = await _restore(client, CHUTES)

    assert restored.status_code == 200, restored.text
    row = {r["route_id"]: r for r in restored.json()["routes"]}[CHUTES]
    assert row["override_applied"] is True
    assert row["capabilities"] == NARROW_CAPABILITIES
    assert row["capabilities_baseline"] == DEFAULT_CAPABILITIES
    adapter = _route_adapter(route_executor, CHUTES)
    assert adapter.config.openrouter_pinned_provider == "parasail"
    _assert_capabilities_values(adapter, NARROW_CAPABILITIES)
    # Its baseline survived the delete and the edit's re-application.
    cleared = await client.delete(chutes_path, headers=AUTH)
    assert cleared.json()["capabilities"] == DEFAULT_CAPABILITIES


@pytest.mark.asyncio
async def test_a_config_route_deleted_at_boot_keeps_its_capability_override(admin_client, caplog):
    """Boot syncs overrides before it applies removals; either order lists and restores it."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    _seed_chutes_edit(op_store)
    services = client.app.state.services
    op_store.capability_rows[("minimax-fast", CHUTES)] = {
        "model_id": "minimax-fast",
        "route_id": CHUTES,
        "capabilities": json.dumps(NARROW_CAPABILITIES),
    }
    await states.set("minimax-fast", CHUTES, disabled=False, removed=True, updated_by="a")
    await states.resolver.load_all()

    with caplog.at_level(logging.WARNING):
        await apply_persisted_provider_route_configs(services, op_store)
        provider_routes.apply_persisted_provider_route_removals(services)
        await provider_routes.sync_route_capability_overrides(services, op_store)

    assert CHUTES not in _live_route_ids(route_executor)
    assert _orphan_warnings(caplog) == []
    listed = await client.get("/admin/routing/provider-routes/minimax-fast", headers=AUTH)
    row = {r["route_id"]: r for r in listed.json()["routes"]}[CHUTES]
    assert row["removed"] is True
    assert row["capabilities"] == NARROW_CAPABILITIES

    restored = await _restore(client, CHUTES)

    assert restored.status_code == 200, restored.text
    _assert_capabilities_values(_route_adapter(route_executor, CHUTES), NARROW_CAPABILITIES)


@pytest.mark.asyncio
async def test_disable_and_enable_keep_a_routes_capability_override(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    weights = _CandidateTable(op_store)
    resolver = _attach_weight_resolver(client, route_executor, weights)
    path = f"/admin/routing/provider-route-capabilities/minimax-fast/{FEATHERLESS}"
    assert (await client.put(path, json=NARROW_CAPABILITIES, headers=AUTH)).status_code == 200

    disabled = await _set_state(client, FEATHERLESS, True)

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["capabilities"] == NARROW_CAPABILITIES
    assert disabled.json()["capabilities_overridden"] is True
    # A disabled route is listed and editable, capabilities included.
    edited = await client.put(path, json=RUNTIME_CAPABILITIES, headers=AUTH)
    assert edited.status_code == 200, edited.text
    assert edited.json()["disabled"] is True
    # Enabling also clears a weight override of 0 left by the old disable.
    resolver.set_override("minimax-fast", FEATHERLESS, 0.0)

    enabled = await _set_state(client, FEATHERLESS, False)

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["capabilities"] == RUNTIME_CAPABILITIES
    assert enabled.json()["capabilities_overridden"] is True
    assert set(op_store.capability_rows) == {("minimax-fast", FEATHERLESS)}
    _assert_capabilities_values(_route_adapter(route_executor, FEATHERLESS), RUNTIME_CAPABILITIES)


# -- A deleted route's saved edit and the endpoint its restore needs ----------


def _live_endpoint_ids(route_executor, model_id: str = "minimax-fast") -> list[str]:
    return [endpoint_id for _a, _w, endpoint_id in route_executor.routes[model_id].raw_adapters]


def _refuse_shared_endpoints(route_executor, model_id: str = "minimax-fast"):
    """Fail a RouteWise refresh, as its candidate build does, when routes share an endpoint."""

    def refresh() -> None:
        endpoint_ids = _live_endpoint_ids(route_executor, model_id)
        if len(set(endpoint_ids)) != len(endpoint_ids):
            raise ValueError(f"duplicate endpoint id in {endpoint_ids}")

    return refresh


@pytest.mark.asyncio
async def test_a_deleted_routes_saved_edit_keeps_its_endpoint_reserved(admin_client):
    """While a route is deleted, no route can take the endpoint its restore serves on."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    table = _CandidateTable(op_store)
    _seed_chutes_edit(op_store)
    await apply_persisted_provider_route_configs(client.app.state.services, op_store)
    assert _live_endpoint_ids(route_executor)[0] == PARASAIL_ENDPOINT_ID
    assert (await _remove(client, CHUTES)).status_code == 200

    added = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "on_demand",
            **OPENROUTER_PARASAIL_TARGET,
            "weight": 1.0,
        },
        headers=AUTH,
    )

    assert added.status_code == 409, added.text
    assert added.json()["detail"] == "provider route already exists"
    assert table.rows == {}
    # Nor can a route already there be moved onto it.
    await _add_minimax_runtime_route(client, op_store)
    moved = await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)
    assert moved.status_code == 409, moved.text
    assert moved.json()["detail"] == (
        f"another route of this model already uses endpoint id {PARASAIL_ENDPOINT_ID!r}"
    )

    restored = await _restore(client, CHUTES)

    assert restored.status_code == 200, restored.text
    assert _live_endpoint_ids(route_executor) == [
        PARASAIL_ENDPOINT_ID,
        FEATHERLESS,
        DEEPINFRA,
        MINIMAX_ROUTE_ID,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_refuses_duplicates", [False, True], ids=["fixed", "routewise"])
async def test_restore_refuses_an_edit_whose_endpoint_another_route_took(
    admin_client, caplog, refresh_refuses_duplicates
):
    """A route that took the endpoint anyway (another process, an older build) wins at boot,
    and the deleted route's restore is refused instead of sharing the endpoint with it."""
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    table = _CandidateTable(op_store)
    if refresh_refuses_duplicates:
        fake_routewise.refresh_route_table.side_effect = _refuse_shared_endpoints(route_executor)
    _seed_chutes_edit(op_store)
    await states.set("minimax-fast", CHUTES, disabled=False, removed=True, updated_by="seed")
    await table.upsert(
        "minimax-fast",
        PARASAIL_ENDPOINT_ID,
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
        RUNTIME_CAPABILITIES,
        "other-admin",
    )
    services = client.app.state.services
    await states.resolver.load_all()

    # Boot, in bootstrap's order: the runtime route is restored before the edit.
    with caplog.at_level(logging.WARNING):
        await provider_routes.apply_persisted_provider_route_candidates(services, op_store)
        await apply_persisted_provider_route_configs(services, op_store)
        endpoint_ids = _live_endpoint_ids(route_executor)
        assert len(set(endpoint_ids)) == len(endpoint_ids), endpoint_ids
        await provider_routes.sync_route_capability_overrides(services, op_store)
        provider_routes.apply_persisted_provider_route_removals(services)

    assert _live_endpoint_ids(route_executor) == [FEATHERLESS, DEEPINFRA, PARASAIL_ENDPOINT_ID]
    assert [
        record.getMessage()
        for record in caplog.records
        if CHUTES in record.getMessage() and "already uses endpoint id" in record.getMessage()
    ]
    # Kept: the edit is skipped while the endpoint is taken, not discarded.
    op_store.delete_provider_route_config.assert_not_awaited()
    before = list(route_executor.routes["minimax-fast"].raw_adapters)

    restored = await _restore(client, CHUTES)

    assert restored.status_code == 409, restored.text
    assert restored.json()["detail"] == (
        f"route {CHUTES!r} cannot be restored: its saved edit serves endpoint id "
        f"{PARASAIL_ENDPOINT_ID!r}, which route {PARASAIL_ENDPOINT_ID!r} now uses. Delete "
        "that route or move it to another endpoint, then restore this one."
    )
    assert states.rows[("minimax-fast", CHUTES)]["removed"] is True
    assert route_executor.routes["minimax-fast"].raw_adapters == before


@pytest.mark.asyncio
async def test_route_state_endpoints_wait_for_the_states_to_load(admin_client):
    """A write over a snapshot that could not be read would clobber the stored flags."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    op_store.list_all_provider_route_states = AsyncMock(
        side_effect=ConnectionError("database unavailable")
    )
    with pytest.raises(ConnectionError):
        await states.resolver.load_all()

    responses = [
        await _set_state(client, FEATHERLESS, True),
        await _remove(client, FEATHERLESS),
        await _restore(client, FEATHERLESS),
    ]

    assert [response.status_code for response in responses] == [503, 503, 503]
    assert {response.json()["detail"] for response in responses} == {
        "Route states are still loading; try again shortly"
    }
    assert states.rows == {}
    assert FEATHERLESS in _live_route_ids(route_executor)

    op_store.list_all_provider_route_states = AsyncMock(side_effect=states.list_all)
    await states.resolver.load_all()
    assert (await _set_state(client, FEATHERLESS, True)).status_code == 200


# -- A limit-only edit changes the limit, not the route -----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{"concurrency_limit": 4}, {**OPENROUTER_PARASAIL_TARGET, "concurrency_limit": 4}],
    ids=["inline-limit", "edit-form"],
)
async def test_a_limit_only_edit_keeps_the_routes_adapter_key_pool_and_price(
    admin_client, monkeypatch, body
):
    """Rebuilding would look the key and the OpenRouter price up again; a failed price
    lookup would leave the route on its model's first route's price."""
    client, op_store, route_executor, fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    added = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "concurrency",
            **OPENROUTER_PARASAIL_TARGET,
            "concurrency_limit": 2,
            "weight": 1.0,
        },
        headers=AUTH,
    )
    assert added.status_code == 200, added.text
    adapter = _live_entry(route_executor, PARASAIL_ENDPOINT_ID)[1]
    assert adapter.config.pricing == OPENROUTER_PARASAIL_PRICING
    key_pool = adapter._key_pool
    fake_routewise.refresh_route_table.reset_mock()
    verify_mock.reset_mock()

    async def unavailable(_model_id):
        raise RuntimeError("openrouter unavailable")

    monkeypatch.setattr(provider_routes, "_fetch_openrouter_endpoint_payload", unavailable)
    # Its key was deleted since: still only the limit changes.
    op_store.get_provider_key_full.return_value = None

    response = await _patch_candidate(client, PARASAIL_ENDPOINT_ID, body)

    assert response.status_code == 200, response.text
    assert response.json()["concurrency_limit"] == 4
    assert _live_entry(route_executor, PARASAIL_ENDPOINT_ID)[1] is adapter
    assert adapter._key_pool is key_pool
    assert adapter.config.concurrency == {"limit": 4}
    assert adapter.config.pricing == OPENROUTER_PARASAIL_PRICING
    assert table.row("minimax-fast", PARASAIL_ENDPOINT_ID)["concurrency_limit"] == 4
    fake_routewise.refresh_route_table.assert_called_once_with()
    verify_mock.assert_not_awaited()
    details = provider_routes.log_admin_action.await_args.kwargs["details"]
    assert (details["old_concurrency_limit"], details["concurrency_limit"]) == (2, 4)


@pytest.mark.asyncio
async def test_a_failed_limit_change_restores_the_limit_and_the_row(admin_client):
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store, route_type="quota", quota_limit=100)
    adapter = _live_entry(route_executor, MINIMAX_ROUTE_ID)[1]
    fake_routewise.refresh_route_table.side_effect = [RuntimeError("rebuild failed"), None]

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await _patch_candidate(client, MINIMAX_ROUTE_ID, {"quota_limit": 250})

    assert _live_entry(route_executor, MINIMAX_ROUTE_ID)[1] is adapter
    assert adapter.config.quota == {"limit": 100}
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID)["quota_limit"] == 100


# -- Edits, deletes and restores that interleave ------------------------------


def _hold_verification(verify_mock, route_id: str | None = None):
    """Make the upstream check of ``route_id`` (default: any route) wait for a release."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def verify(candidate):
        if route_id is None or candidate.route_id == route_id:
            entered.set()
            await release.wait()

    verify_mock.side_effect = verify
    return entered, release


def _real_routewise(client, route_executor) -> RouteWiseRouter:
    """Rebuild a real RouteWise router from the route table on every admin refresh."""
    routewise = RouteWiseRouter(route_table=route_executor)
    registry = client.app.state.services.model_router_registry
    registry.refresh_route_tables.side_effect = routewise.refresh_route_table
    return routewise


def _routewise_endpoints(routewise: RouteWiseRouter, model_id: str = "minimax-fast") -> set[str]:
    return {candidate.endpoint_id for candidate in routewise.route_candidates[model_id]}


@pytest.mark.asyncio
@pytest.mark.parametrize("shift", ["delete", "restore"])
async def test_config_route_edit_lands_on_its_route_after_an_earlier_route_moves(
    admin_client, shift
):
    """An edit names its route by id: deleting or restoring the route ahead of it
    while the edit is verified moves every route after it."""
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    if shift == "restore":
        assert (await _remove(client, CHUTES)).status_code == 200
    entered, release = _hold_verification(verify_mock)
    edit = asyncio.create_task(
        client.put(
            f"/admin/routing/provider-routes/minimax-fast/{FEATHERLESS}",
            json=OPENROUTER_PARASAIL_TARGET,
            headers=AUTH,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)

    moved = await (_remove(client, CHUTES) if shift == "delete" else _restore(client, CHUTES))
    assert moved.status_code == 200, moved.text
    release.set()
    response = await edit

    assert response.status_code == 200, response.text
    served = dict(
        zip(_live_route_ids(route_executor), _live_endpoint_ids(route_executor), strict=False)
    )
    if shift == "delete":
        assert served == {FEATHERLESS: PARASAIL_ENDPOINT_ID, DEEPINFRA: DEEPINFRA}
    else:
        assert served == {CHUTES: CHUTES, FEATHERLESS: PARASAIL_ENDPOINT_ID, DEEPINFRA: DEEPINFRA}
        assert _route_adapter(route_executor, CHUTES).config.provider == "chutes"


@pytest.mark.asyncio
async def test_config_route_edit_rechecks_its_endpoint_after_verification(admin_client):
    """A route added while the edit is verified may take the endpoint it moves to."""
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    _CandidateTable(op_store)
    op_store.get_provider_key_full.return_value = ("openrouter", "openrouter-db-key-1234567890")
    entered, release = _hold_verification(verify_mock, FEATHERLESS)
    edit = asyncio.create_task(
        client.put(
            f"/admin/routing/provider-routes/minimax-fast/{FEATHERLESS}",
            json=OPENROUTER_PARASAIL_TARGET,
            headers=AUTH,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    added = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "on_demand",
            **OPENROUTER_PARASAIL_TARGET,
            "weight": 1.0,
        },
        headers=AUTH,
    )
    assert added.status_code == 200, added.text
    before = list(route_executor.routes["minimax-fast"].raw_adapters)

    release.set()
    response = await edit

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        f"another route of this model already uses endpoint id {PARASAIL_ENDPOINT_ID!r}"
    )
    op_store.upsert_provider_route_config.assert_not_awaited()
    assert route_executor.routes["minimax-fast"].raw_adapters == before


@pytest.mark.asyncio
async def test_runtime_route_edit_rechecks_its_endpoint_before_saving(admin_client):
    """The same for a runtime route: refused before its row is rewritten."""
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    await _add_minimax_runtime_route(client, op_store)
    before_row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    entered, release = _hold_verification(verify_mock, MINIMAX_ROUTE_ID)
    edit = asyncio.create_task(
        _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)
    )
    await asyncio.wait_for(entered.wait(), 5)
    added = await client.post(
        "/admin/routing/provider-route-candidates/minimax-fast",
        json={
            "capabilities": RUNTIME_CAPABILITIES,
            "route_type": "on_demand",
            **OPENROUTER_PARASAIL_TARGET,
            "weight": 1.0,
        },
        headers=AUTH,
    )
    assert added.status_code == 200, added.text

    release.set()
    response = await edit

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        f"another route of this model already uses endpoint id {PARASAIL_ENDPOINT_ID!r}"
    )
    op_store.update_provider_route_candidate.assert_not_awaited()
    assert table.row("minimax-fast", MINIMAX_ROUTE_ID) == before_row
    assert _live_entry(route_executor, MINIMAX_ROUTE_ID)[3] == MINIMAX_ROUTE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_field", ["quota_limit", "concurrency_limit"])
async def test_a_target_edit_verified_across_a_limit_change_does_not_undo_it(
    admin_client, limit_field
):
    """A limit-only edit keeps the adapter, so the edit being verified must notice
    the limit it was built from is gone rather than write it back."""
    client, op_store, route_executor, _fake_routewise, verify_mock = admin_client
    table = _CandidateTable(op_store)
    route_type = "quota" if limit_field == "quota_limit" else "concurrency"
    await _add_minimax_runtime_route(client, op_store, route_type=route_type, **{limit_field: 100})
    entered, release = _hold_verification(verify_mock, MINIMAX_ROUTE_ID)
    moving = asyncio.create_task(
        _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)
    )
    await asyncio.wait_for(entered.wait(), 5)
    changed = await _patch_candidate(client, MINIMAX_ROUTE_ID, {limit_field: 250})
    assert changed.status_code == 200, changed.text

    release.set()
    moved = await moving

    assert moved.status_code == 409, moved.text
    assert moved.json()["detail"] == "provider route changed during verification"
    row = table.row("minimax-fast", MINIMAX_ROUTE_ID)
    assert (row["provider"], row[limit_field]) == ("minimax", 250)
    _index, adapter, _weight, endpoint_id = _live_entry(route_executor, MINIMAX_ROUTE_ID)
    assert endpoint_id == MINIMAX_ROUTE_ID
    limit_of = (
        provider_routes._quota_limit_for_adapter
        if limit_field == "quota_limit"
        else provider_routes._concurrency_limit_for_adapter
    )
    assert limit_of(adapter) == 250


@pytest.mark.asyncio
async def test_a_moved_runtime_route_keeps_its_zero_weight_through_the_routewise_rebuild(
    admin_client,
):
    """The weight override moves with the endpoint before RouteWise is rebuilt, so the
    route never becomes a candidate the database and the console say has weight 0."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    table = _CandidateTable(op_store)
    weights = _attach_weight_resolver(client, route_executor, table)
    await _add_minimax_runtime_route(client, op_store)
    table.weights[("minimax-fast", MINIMAX_ROUTE_ID)] = 0.0
    weights.set_override("minimax-fast", MINIMAX_ROUTE_ID, 0.0)
    routewise = _real_routewise(client, route_executor)
    assert MINIMAX_ROUTE_ID not in _routewise_endpoints(routewise)

    response = await _patch_candidate(client, MINIMAX_ROUTE_ID, OPENROUTER_PARASAIL_TARGET)

    assert response.status_code == 200, response.text
    assert weights.get_snapshot_for_model("minimax-fast") == {PARASAIL_ENDPOINT_ID: 0.0}
    assert PARASAIL_ENDPOINT_ID not in _routewise_endpoints(routewise)
    # The stored weights match what was published, so a poll rebuilds nothing.
    registry = client.app.state.services.model_router_registry
    assert not await _reload_effective_route_state(weights, registry, _EffectiveRouteRefreshState())
    assert PARASAIL_ENDPOINT_ID not in _routewise_endpoints(routewise)


@pytest.mark.asyncio
async def test_a_restored_route_is_a_routewise_candidate_again_without_another_change(
    admin_client,
):
    """The restore publishes the route state before the rebuild that adds the route, so
    an unchanged route-state poll afterwards has nothing left to fix."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    routewise = _real_routewise(client, route_executor)
    assert FEATHERLESS in _routewise_endpoints(routewise)
    assert (await _remove(client, FEATHERLESS)).status_code == 200
    assert FEATHERLESS not in _routewise_endpoints(routewise)

    restored = await _restore(client, FEATHERLESS)

    assert restored.status_code == 200, restored.text
    assert FEATHERLESS in _routewise_endpoints(routewise)
    registry = client.app.state.services.model_router_registry
    assert not await _reload_effective_route_state(
        states.resolver, registry, _EffectiveRouteRefreshState()
    )
    assert FEATHERLESS in _routewise_endpoints(routewise)


@pytest.mark.asyncio
async def test_a_failed_restore_leaves_the_route_deleted_in_every_rebuild(admin_client):
    """Each RouteWise rebuild sees the route and its state agree: restored while it is
    in the table, deleted again once the failed restore takes it back out."""
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    assert (await _remove(client, FEATHERLESS)).status_code == 200
    seen: list[tuple[bool, bool]] = []

    def refresh() -> None:
        seen.append(
            (
                FEATHERLESS in _live_route_ids(route_executor),
                states.resolver.get("minimax-fast", FEATHERLESS).removed,
            )
        )
        if len(seen) == 1:
            raise RuntimeError("rebuild failed")

    fake_routewise.refresh_route_table.side_effect = refresh

    with pytest.raises(RuntimeError, match="rebuild failed"):
        await _restore(client, FEATHERLESS)

    assert seen == [(True, False), (False, True)]
    assert FEATHERLESS not in _live_route_ids(route_executor)
    assert states.rows[("minimax-fast", FEATHERLESS)]["removed"] is True
    assert states.resolver.get("minimax-fast", FEATHERLESS).removed


@pytest.mark.asyncio
async def test_restore_never_serves_the_yaml_target_of_a_route_with_a_saved_edit(
    admin_client, monkeypatch
):
    """The saved edit is built while the route is still out, and the route comes back
    already on it: no request and no RouteWise rebuild sees its models.yaml target."""
    client, op_store, route_executor, fake_routewise, _verify_mock = admin_client
    _RouteStateTable(client, op_store, route_executor)
    _seed_chutes_edit(op_store)
    await apply_persisted_provider_route_configs(client.app.state.services, op_store)
    assert (await _remove(client, CHUTES)).status_code == 200
    selectable_on: list[str] = []

    def record_selectable_targets() -> None:
        if _effective_weights(route_executor).get(CHUTES, 0.0) > 0:
            selectable_on.append(_route_adapter(route_executor, CHUTES).config.provider)

    prepare = provider_routes._prepare_route_update

    async def observed_prepare(*args, **kwargs):
        record_selectable_targets()
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(provider_routes, "_prepare_route_update", observed_prepare)
    fake_routewise.refresh_route_table.side_effect = record_selectable_targets

    restored = await _restore(client, CHUTES)

    assert restored.status_code == 200, restored.text
    assert {r["route_id"]: r for r in restored.json()["routes"]}[CHUTES]["override_applied"]
    assert selectable_on and set(selectable_on) == {"openrouter"}
    assert _route_adapter(route_executor, CHUTES).config.openrouter_pinned_provider == "parasail"


@pytest.mark.asyncio
async def test_an_enable_from_a_stale_snapshot_cannot_undo_another_process_delete(admin_client):
    """Both flags are written from this process's snapshot, so the write only lands
    while the stored state is still the one the snapshot shows."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    assert (await _set_state(client, FEATHERLESS, True)).status_code == 200
    # A second gateway process, whose snapshot was read before the delete below.
    other_router = FixedRouter()
    other_router.register_route(
        "minimax-fast", list(route_executor.routes["minimax-fast"].adapters)
    )
    other_states = RouteStateResolver(op_store)
    await other_states.load_all()
    other_router.route_state_resolver = other_states
    assert (await _remove(client, FEATHERLESS)).status_code == 200
    services = client.app.state.services
    services.router = other_router
    services.route_state_resolver = other_states

    response = await _set_state(client, FEATHERLESS, False)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        f"route {FEATHERLESS!r} was changed from another gateway process; reload and try again"
    )
    stored = states.rows[("minimax-fast", FEATHERLESS)]
    assert (stored["disabled"], stored["removed"]) == (True, True)
    assert other_states.get("minimax-fast", FEATHERLESS) == RouteState(disabled=True)
    provider_routes.log_admin_action.assert_awaited()
    assert _audited_actions()[-1] == "routing.provider_routes.remove"


@pytest.mark.asyncio
async def test_a_delete_from_a_stale_snapshot_keeps_another_process_disable(admin_client):
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    # Another gateway process disabled the route; this one has not reloaded yet.
    await states.set("minimax-fast", FEATHERLESS, disabled=True, removed=False, updated_by="b")

    response = await _remove(client, FEATHERLESS)

    assert response.status_code == 409, response.text
    stored = states.rows[("minimax-fast", FEATHERLESS)]
    assert (stored["disabled"], stored["removed"]) == (True, False)
    assert FEATHERLESS in _live_route_ids(route_executor)
    assert not states.resolver.is_excluded("minimax-fast", FEATHERLESS)


@pytest.mark.asyncio
async def test_a_route_marked_deleted_but_still_listed_cannot_be_toggled(admin_client):
    """A removal not yet applied to this table: enabling it would only rewrite the mark."""
    client, op_store, route_executor, _fake_routewise, _verify_mock = admin_client
    states = _RouteStateTable(client, op_store, route_executor)
    await states.set("minimax-fast", FEATHERLESS, disabled=True, removed=True, updated_by="b")
    await states.resolver.load_all()
    assert FEATHERLESS in _live_route_ids(route_executor)

    response = await _set_state(client, FEATHERLESS, False)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        f"route {FEATHERLESS!r} is deleted from this deployment; restore it first"
    )
    stored = states.rows[("minimax-fast", FEATHERLESS)]
    assert (stored["disabled"], stored["removed"]) == (True, True)
