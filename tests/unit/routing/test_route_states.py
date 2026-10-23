"""Per-route admin disable in FixedRouter (``serving.config.route_states``).

A disabled route keeps its configuration and weights but is selected by
nothing: not the weighted draw, not a fallback after a failure, not the
offload route, and not RouteWise's candidate set. Enabling it again restores
the weight it had.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from routing.offload import OffloadPolicy
from routing.routers import EXCLUSION_ROUTE_DISABLED, AllCircuitsOpenError, FixedRouter
from routing.routewise.candidates import build_provider_candidates
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.config.route_states import ENABLED, RouteState, RouteStateResolver

MODEL = "m"
MESSAGES = [{"role": "user", "content": "hi"}]


class _Adapter(BaseAdapter):
    def __init__(self, name: str, *, fail: bool = False) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider=name,
                base_url=f"https://{name}.example/v1",
                endpoint_id=f"{MODEL}:{name}-api",
                route_metadata={"route_id": f"route-{name}"},
            )
        )
        self.fail = fail
        self.calls = 0

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any):
        self.calls += 1
        if self.fail:
            raise RuntimeError("upstream 500")
        return self.format_response(content=self.config.provider, model=MODEL)

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params: Any):
        self.calls += 1
        yield self.format_stream_chunk(content=self.config.provider, model=MODEL)


class _Policies:
    def __init__(self, route_id: str) -> None:
        self.policy = OffloadPolicy(route_id=route_id, wait_seconds=0.05)

    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        return self.policy if model_id == MODEL else None


class _Weights:
    def __init__(self, overrides: dict[str, float]) -> None:
        self.overrides = overrides

    def get_snapshot_for_model(self, model_id: str) -> dict[str, float]:
        return dict(self.overrides) if model_id == MODEL else {}


def _states(*disabled_route_ids: str) -> RouteStateResolver:
    resolver = RouteStateResolver(store=None)
    for route_id in disabled_route_ids:
        resolver.set_state(MODEL, route_id, RouteState(disabled=True))
    return resolver


@pytest.fixture(autouse=True)
def _first_draw_wins(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.0)


def test_a_disabled_route_is_never_drawn():
    first, second = _Adapter("first"), _Adapter("second")
    router = FixedRouter(route_state_resolver=_states("route-first"))
    router.register_route(MODEL, [(first, 1.0), (second, 1.0)])

    for _ in range(20):
        assert router.select_adapter(MODEL) is second


def test_disabling_every_route_leaves_nothing_to_select():
    first = _Adapter("first")
    router = FixedRouter(route_state_resolver=_states("route-first"))
    router.register_route(MODEL, [(first, 1.0)])

    with pytest.raises(AllCircuitsOpenError):
        router.select_adapter(MODEL)


async def test_a_disabled_route_is_not_a_fallback():
    primary = _Adapter("primary", fail=True)
    disabled = _Adapter("disabled")
    backup = _Adapter("backup")
    router = FixedRouter(route_state_resolver=_states("route-disabled"))
    router.register_route(MODEL, [(primary, 1.0), (disabled, 1.0), (backup, 1.0)])

    resp = await router.chat_completion(MODEL, MESSAGES)

    assert resp["choices"][0]["message"]["content"] == "backup"
    assert disabled.calls == 0
    assert [a["endpoint_id"] for a in resp["_routing"]["failed_attempts"]] == ["m:primary-api"]


async def test_a_disabled_route_is_not_the_offload_route():
    primary = _Adapter("primary", fail=True)
    reserved = _Adapter("reserved")
    states = RouteStateResolver(store=None)
    router = FixedRouter(
        offload_policy_resolver=_Policies("route-reserved"),
        route_state_resolver=states,
    )
    router.register_route(MODEL, [(primary, 1.0), (reserved, 1.0)])

    # Enabled, the offload route is the last resort once the primary fails.
    resp = await router.chat_completion(MODEL, MESSAGES)
    assert resp["choices"][0]["message"]["content"] == "reserved"
    assert reserved.calls == 1

    states.set_state(MODEL, "route-reserved", RouteState(disabled=True))
    with pytest.raises(RuntimeError, match="upstream 500"):
        await router.chat_completion(MODEL, MESSAGES)
    assert reserved.calls == 1


def test_a_disabled_route_leaves_the_routewise_candidate_set():
    first, second = _Adapter("first"), _Adapter("second")
    router = FixedRouter(route_state_resolver=_states("route-second"))
    router.register_route(MODEL, [(first, 1.0), (second, 1.0)])

    (effective,) = router.iter_effective_routes()
    candidates = build_provider_candidates(MODEL, effective.adapters)

    assert [candidate.endpoint_id for candidate in candidates] == ["m:first-api"]


def test_enabling_restores_the_weight_the_route_had():
    first, second = _Adapter("first"), _Adapter("second")
    states = _states("route-second")
    router = FixedRouter(
        weight_override_resolver=_Weights({"m:second-api": 3.0}),
        route_state_resolver=states,
    )
    router.register_route(MODEL, [(first, 1.0), (second, 1.0)])

    (disabled,) = router.iter_effective_routes()
    assert [weight for _adapter, weight in disabled.adapters] == [1.0, 0.0]

    states.set_state(MODEL, "route-second", ENABLED)

    (enabled,) = router.iter_effective_routes()
    assert [weight for _adapter, weight in enabled.adapters] == [1.0, 3.0]


def test_a_disabled_route_is_reported_as_an_exclusion():
    first, second = _Adapter("first"), _Adapter("second")
    router = FixedRouter(route_state_resolver=_states("route-second"))
    router.register_route(MODEL, [(first, 1.0), (second, 1.0)])

    (fact,) = router.get_route_exclusions()

    assert fact["endpoint_id"] == "m:second-api"
    assert fact["reasons"] == [EXCLUSION_ROUTE_DISABLED]


async def test_the_resolver_loads_disabled_and_removed_routes():
    store = SimpleNamespace(
        list_all_provider_route_states=AsyncMock(
            return_value=[
                {"model_id": MODEL, "route_id": "a", "disabled": True, "removed": False},
                {"model_id": MODEL, "route_id": "b", "disabled": False, "removed": True},
                {"model_id": MODEL, "route_id": "c", "disabled": False, "removed": False},
            ]
        )
    )
    resolver = RouteStateResolver(store)

    assert await resolver.load_all() is True
    assert resolver.is_excluded(MODEL, "a")
    assert resolver.is_excluded(MODEL, "b")
    assert not resolver.is_excluded(MODEL, "c")
    assert resolver.removed_routes() == [(MODEL, "b")]
    assert await resolver.load_all() is False

    resolver.clear_model(MODEL)
    assert not resolver.is_excluded(MODEL, "a")
