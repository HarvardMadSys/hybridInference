"""Switching a model to RouteWise is refused while its route weights mean something.

RouteWise does not split traffic by weight: every route weighted above 0 is a
full candidate. Switching any model with a 1% staging leg to ``routewise`` made
the staging route a full peer -- in the review's repro it took 200 of 200
picks -- so production prompts went to staging. ``optional: true`` cannot mark
a canary at runtime: it only decides whether a route whose environment is
unset is skipped at startup.
"""

from __future__ import annotations

import pytest

from routing.model_router_registry import ModelRouterRegistry
from serving.servers.deps import AppServices
from serving.servers.routers.admin.provider_routes import (
    MODEL_ROUTER_STRATEGY_SETTING_PREFIX,
    apply_persisted_model_router_strategy_overrides,
)
from tests.servers.test_admin_provider_routes import (  # noqa: F401  (fixture import)
    AUTH,
    _compat_adapter,
    admin_client,
)

MODEL = "canary-model"
STAGING = f"{MODEL}:staging-api"


def _register(route_executor, *, staging_weight: float, prod_b_weight: float = 1.0) -> None:
    adapters = [
        _compat_adapter(
            model_id=MODEL,
            provider="openrouter",
            endpoint_id=f"{MODEL}:prod-a-api",
            base_url="https://openrouter.ai/api/v1",
            provider_model_id="openai/gpt-oss-20b",
        ),
        _compat_adapter(
            model_id=MODEL,
            provider="openrouter",
            endpoint_id=f"{MODEL}:prod-b-api",
            base_url="https://openrouter.ai/api/v1",
            provider_model_id="openai/gpt-oss-20b",
        ),
        _compat_adapter(
            model_id=MODEL,
            provider="openrouter",
            endpoint_id=STAGING,
            base_url="https://openrouter.ai/api/v1",
            provider_model_id="openai/gpt-oss-20b",
        ),
    ]
    route_executor.register_route(
        MODEL, list(zip(adapters, (1.0, prod_b_weight, staging_weight), strict=True))
    )


class _WeightOverrides:
    """A weight-override resolver double, as the admin weights tab installs one."""

    def __init__(self, overrides: dict[str, dict[str, float]]) -> None:
        self._overrides = overrides

    def get_snapshot_for_model(self, model_id: str) -> dict[str, float]:
        return dict(self._overrides.get(model_id, {}))


async def _switch(client, strategy: str):
    return await client.patch(
        f"/admin/routing/provider-route-strategies/{MODEL}",
        json={"strategy": strategy},
        headers=AUTH,
    )


@pytest.mark.asyncio
async def test_a_switch_to_routewise_is_refused_while_a_canary_route_exists(
    admin_client,  # noqa: F811
):
    client, op_store, route_executor, _fake_routewise, _verify = admin_client
    _register(route_executor, staging_weight=0.01)
    assert (await _switch(client, "fixed")).status_code == 200
    op_store.set_setting.reset_mock()

    response = await _switch(client, "routewise")

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert f"{STAGING} (1% of the heaviest route's weight)" in detail
    assert "Set their weight to 0" in detail
    assert op_store.set_setting.await_count == 0
    listing = await client.get(f"/admin/routing/provider-routes/{MODEL}", headers=AUTH)
    assert listing.json()["strategy"] == "fixed"


@pytest.mark.asyncio
async def test_unequal_production_weights_are_refused_too(admin_client):  # noqa: F811
    client, _op_store, route_executor, _fake_routewise, _verify = admin_client
    _register(route_executor, staging_weight=1.0, prod_b_weight=0.3)
    assert (await _switch(client, "fixed")).status_code == 200

    response = await _switch(client, "routewise")

    assert response.status_code == 422
    assert f"{MODEL}:prod-b-api (30% of the heaviest route's weight)" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("zeroed_by", ["models.yaml", "weight override"])
async def test_a_switch_is_allowed_once_the_canary_is_weighted_0(
    admin_client,  # noqa: F811
    zeroed_by,
):
    client, _op_store, route_executor, _fake_routewise, _verify = admin_client
    if zeroed_by == "models.yaml":
        _register(route_executor, staging_weight=0.0)
    else:
        _register(route_executor, staging_weight=0.01)
        route_executor.weight_override_resolver = _WeightOverrides({MODEL: {STAGING: 0.0}})
    assert (await _switch(client, "fixed")).status_code == 200

    response = await _switch(client, "routewise")

    assert response.status_code == 200, response.text
    assert response.json()["strategy"] == "routewise"


@pytest.mark.asyncio
async def test_a_model_already_on_routewise_is_not_refused(admin_client):  # noqa: F811
    client, _op_store, route_executor, _fake_routewise, _verify = admin_client
    _register(route_executor, staging_weight=0.01)

    # The fixture's registry serves models on routewise unless switched.
    response = await _switch(client, "routewise")

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_a_persisted_switch_is_still_applied_at_startup(admin_client):  # noqa: F811
    """Refusing there would silently move a running model on its next restart."""
    _client, op_store, route_executor, _fake_routewise, _verify = admin_client
    _register(route_executor, staging_weight=0.01)
    registry = ModelRouterRegistry(
        models_config={MODEL: {"router": "fixed"}},
        shared_fixed_router=route_executor,
    )
    services = AppServices(
        router=route_executor,
        model_router_registry=registry,
        operational_store=op_store,
    )
    op_store.list_settings.return_value = [
        {"key": f"{MODEL_ROUTER_STRATEGY_SETTING_PREFIX}{MODEL}", "value": "routewise"}
    ]

    await apply_persisted_model_router_strategy_overrides(services, op_store)

    assert registry.get_router_name(MODEL) == "routewise"
