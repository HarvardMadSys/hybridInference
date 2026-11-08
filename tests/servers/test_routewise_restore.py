"""A config restore leaves every RouteWise model on a started, tracked router.

The restore reconciles the live route table with the restored store: it resets
each model to its configured router, then re-applies the persisted strategy
overrides and runtime routes. A ``router: routewise`` model was detached even
when nothing overrode it, so its warm router was stopped and the next request
built a replacement lazily: never started (no quota refresh loop, so its quota
legs were never ready and traffic went to paid legs), never attached to the
operational store, and not tracked for shutdown. Routers the persisted
overrides built were left unstarted the same way.
"""

from __future__ import annotations

import time

import pytest

from routing.model_router_registry import ModelRouterRegistry
from routing.routewise.router import RouteWiseRouter
from serving.servers.deps import AppServices
from serving.servers.routers.admin.provider_routes import (
    MODEL_ROUTER_STRATEGY_SETTING_PREFIX,
    _apply_model_router_strategy,
    reconcile_live_routes_from_store,
)
from tests.servers.test_admin_provider_routes import (  # noqa: F401  (fixture import)
    _register_on_demand_only_route,
    admin_client,
)


def _services(route_executor, op_store, models_config) -> tuple[AppServices, ModelRouterRegistry]:
    registry = ModelRouterRegistry(
        models_config=models_config,
        shared_fixed_router=route_executor,
    )
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        operational_store=op_store,
    )
    return services, registry


def _assert_serving(router, services, op_store) -> None:
    assert isinstance(router, RouteWiseRouter)
    assert router._lifecycle_started is True
    assert router._operational_store is op_store
    assert router in services.managed_routers


@pytest.mark.asyncio
async def test_a_routewise_model_keeps_its_warm_router(admin_client):  # noqa: F811
    _client, op_store, route_executor, _fake, _verify = admin_client
    _register_on_demand_only_route(route_executor, model_id="rw-model")
    services, registry = _services(route_executor, op_store, {"rw-model": {"router": "routewise"}})
    booted = registry.get_router("rw-model")
    booted.attach_operational_store(op_store)
    await booted.start()
    services.managed_routers.append(booted)
    booted._latency_profiles["rw-model:openrouter-api"].record(time.time(), 250.0)

    await reconcile_live_routes_from_store(services, op_store)

    live = registry.get_router("rw-model")
    assert live is booted
    _assert_serving(live, services, op_store)
    # What it learned is still there.
    assert live._latency_profiles["rw-model:openrouter-api"].sample_count(time.time()) == 1
    await live.stop()


@pytest.mark.asyncio
async def test_a_model_returned_to_routewise_is_served_by_a_started_router(
    admin_client,  # noqa: F811
):
    _client, op_store, route_executor, _fake, _verify = admin_client
    _register_on_demand_only_route(route_executor, model_id="rw-model")
    services, registry = _services(route_executor, op_store, {"rw-model": {"router": "routewise"}})
    booted = registry.get_router("rw-model")
    booted.attach_operational_store(op_store)
    await booted.start()
    services.managed_routers.append(booted)
    # An administrator switched it to fixed; the restored store has no
    # override, so the restore puts it back on its configured router.
    await _apply_model_router_strategy(services, "rw-model", "fixed")
    assert registry.get_router_name("rw-model") == "fixed"

    await reconcile_live_routes_from_store(services, op_store)

    live = registry.get_router("rw-model")
    assert registry.get_router_name("rw-model") == "routewise"
    _assert_serving(live, services, op_store)
    await live.stop()


@pytest.mark.asyncio
async def test_a_restored_routewise_override_is_started(admin_client):  # noqa: F811
    _client, op_store, route_executor, _fake, _verify = admin_client
    _register_on_demand_only_route(route_executor, model_id="fx-model")
    services, registry = _services(route_executor, op_store, {"fx-model": {"router": "fixed"}})
    op_store.list_settings.return_value = [
        {"key": f"{MODEL_ROUTER_STRATEGY_SETTING_PREFIX}fx-model", "value": "routewise"}
    ]

    await reconcile_live_routes_from_store(services, op_store)

    live = registry.get_router("fx-model")
    assert registry.get_router_name("fx-model") == "routewise"
    _assert_serving(live, services, op_store)
    await live.stop()
