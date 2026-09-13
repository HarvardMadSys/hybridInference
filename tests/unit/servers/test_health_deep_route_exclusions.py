"""``/health/deep`` must surface routes selection can never pick.

The RCA's model had six configured routes and one live one; the deep-health
payload showed nothing about the other five, because its provider map is built
from the endpoint-health registry and a route that is never dispatched to never
appears there.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastapi import Response

from routing.routers import EXCLUSION_PROVIDER_DISABLED, EXCLUSION_WEIGHT_OVERRIDE, FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.routers.health import deep_health


class _EchoAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params):
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _SnapshotWeightResolver:
    def __init__(self, overrides: dict[str, dict[str, float]]) -> None:
        self.overrides = overrides

    def get_snapshot_for_model(self, model_id: str) -> dict[str, float]:
        return dict(self.overrides.get(model_id, {}))


class _StaticDisabledResolver:
    def __init__(self, disabled: set[str]) -> None:
        self.disabled = disabled

    def is_disabled(self, provider: str) -> bool:
        return provider in self.disabled


def _adapter(provider: str) -> _EchoAdapter:
    return _EchoAdapter(
        ModelConfig(
            id="deepseek-v4-flash",
            name="deepseek-v4-flash",
            provider=provider,
            base_url=f"http://{provider}.test/v1",
            context_length=8192,
            max_output_length=1024,
            endpoint_id=f"{provider}:host:443",
        )
    )


async def test_deep_health_names_the_excluded_routes_and_their_causes():
    router = FixedRouter(
        weight_override_resolver=_SnapshotWeightResolver(
            {"deepseek-v4-flash": {"local-8005:host:443": 0.0}}
        ),
        disabled_provider_resolver=_StaticDisabledResolver({"deepseek"}),
    )
    router.register_route(
        "deepseek-v4-flash",
        [(_adapter("local-8004"), 1.0), (_adapter("local-8005"), 1.0), (_adapter("deepseek"), 1.0)],
    )
    response = Response()

    body = await deep_health(
        response,
        router_exec=router,
        services=SimpleNamespace(routing_manager=None),
        op_store=None,
        log_store=None,
    )

    exclusions = {fact["endpoint_id"]: fact for fact in body["route_exclusions"]}
    assert exclusions["local-8005:host:443"]["reasons"] == [EXCLUSION_WEIGHT_OVERRIDE]
    assert exclusions["deepseek:host:443"]["reasons"] == [EXCLUSION_PROVIDER_DISABLED]
    assert body["providers"]["deepseek:host:443"]["excluded_from_models"] == ["deepseek-v4-flash"]
    # An operator zeroing a route is a decision, not an outage: degrading here
    # would page for a gateway that is serving every request it is asked to.
    assert body["status"] == "healthy"
    assert response.status_code == 200


async def test_a_router_without_the_report_still_answers():
    """RouteWise and any other router implement no exclusion reporting."""

    class _StubRouter:
        routes: ClassVar[dict[str, Any]] = {}

        def get_provider_status(self) -> dict[str, dict[str, Any]]:
            return {}

    body = await deep_health(
        Response(),
        router_exec=_StubRouter(),
        services=SimpleNamespace(routing_manager=None),
        op_store=None,
        log_store=None,
    )

    assert body["route_exclusions"] == []


@pytest.mark.unit
def test_route_exclusions_are_json_serializable():
    import json

    router = FixedRouter(disabled_provider_resolver=_StaticDisabledResolver({"deepseek"}))
    router.register_route(
        "deepseek-v4-flash", [(_adapter("local-8004"), 1.0), (_adapter("deepseek"), 1.0)]
    )

    assert json.loads(json.dumps(router.get_route_exclusions()))
