"""The real registry entry point builds the hybrid composition.

These tests do not instantiate ``HybridRouter`` by hand. They build a
``ModelRouterRegistry`` the way ``bootstrap`` does -- shared ``FixedRouter``,
``models.yaml``-shaped config, hybrid factory attached -- and assert on the
routers that entry point actually hands back, so a broken migration fails here
rather than passing beside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from routing.backends import FixedCloudBackend, LocalBackend
from routing.hybrid import HybridRouter
from routing.model_router_registry import ModelRouterRegistry
from routing.policies import FixedPolicy
from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.hybrid_composition import HybridFixedRouterFactory, local_endpoint_scope

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "compose-model"
_MESSAGES = [{"role": "user", "content": "hello"}]
_LOCAL_URL = "http://localhost:11434/v1"
_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-11434"
_CLOUD_ENDPOINT = f"{_MODEL_ID}:zai-api"


class _ComposeAdapter(BaseAdapter):
    """Recording adapter that can fail on demand."""

    def __init__(self, config: ModelConfig, *, chat_error: BaseException | None = None) -> None:
        super().__init__(config)
        self.chat_error = chat_error
        self.chat_calls = 0

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        self.chat_calls += 1
        if self.chat_error is not None:
            raise self.chat_error
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        raise NotImplementedError


def _adapter(
    endpoint_id: str,
    *,
    provider: str,
    base_url: str,
    chat_error: BaseException | None = None,
) -> _ComposeAdapter:
    config = ModelConfig(
        id=_MODEL_ID,
        name=_MODEL_ID,
        provider=provider,
        base_url=base_url,
        endpoint_id=endpoint_id,
        pricing={"prompt": "1", "completion": "1"},
        input_modalities=["text"],
    )
    return _ComposeAdapter(config, chat_error=chat_error)


def _shared_router(*adapters: _ComposeAdapter, health: Any = None) -> FixedRouter:
    from routing.endpoint_health import EndpointHealthRegistry

    router = FixedRouter(health_registry=health if health is not None else EndpointHealthRegistry())
    router.register_route(_MODEL_ID, [(adapter, 1.0) for adapter in adapters])
    return router


def _registry(shared: FixedRouter) -> ModelRouterRegistry:
    """Build the registry exactly as bootstrap does, factory included.

    The health registry comes off the shared router, which is what bootstrap
    passes to the factory: one process-scoped collaborator, so both domains
    report into the same circuit state the shared router uses.
    """
    registry = ModelRouterRegistry(
        models_config={_MODEL_ID: {"router": "fixed"}},
        default_router_name="fixed",
        shared_fixed_router=shared,
    )
    factory = HybridFixedRouterFactory(
        registry=registry,
        health_registry=shared.endpoint_health_registry,
    )
    registry.set_hybrid_router_factory(factory)
    return registry


@pytest.mark.unit
def test_local_endpoint_scope_uses_the_gateway_locality_predicate() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")

    scope = local_endpoint_scope(_shared_router(local, remote), _MODEL_ID)

    assert scope == frozenset({_LOCAL_ENDPOINT})


@pytest.mark.unit
def test_registry_builds_a_hybrid_router_for_a_mixed_model() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")

    router = _registry(_shared_router(local, remote)).get_router(_MODEL_ID)

    assert isinstance(router, HybridRouter)
    assert isinstance(router.policy, FixedPolicy)
    backends = {backend.name: backend for backend in router.backends()}
    assert isinstance(backends["local"], LocalBackend)
    assert isinstance(backends["cloud"], FixedCloudBackend)
    assert backends["local"].endpoint_scope == frozenset({_LOCAL_ENDPOINT})
    assert backends["cloud"].endpoint_scope == frozenset({_CLOUD_ENDPOINT})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_domain_falls_back_to_the_other_domain() -> None:
    """The behavior the single FixedRouter provided by walking one route.

    Neither execution domain can see the other's candidates, so this step has to
    happen at the hybrid layer. What must survive is the outcome: the request
    succeeds on the other side, and its routing metadata reports both attempts.
    """
    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("local down"),
    )
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    # Overwhelming local weight makes the preference deterministic.
    shared = _shared_router(local, remote)
    shared.register_route(_MODEL_ID, [(local, 1e9), (remote, 1.0)])
    router = _registry(shared).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="cross-domain")

    routing = response["_routing"]
    assert routing["backend"] == "cloud"
    assert routing["endpoint_id"] == _CLOUD_ENDPOINT
    assert routing["fallback"] is True
    assert [attempt["backend"] for attempt in routing["failed_attempts"]] == ["local"]
    assert local.chat_calls == 1
    assert remote.chat_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_spot_stays_inside_its_domain() -> None:
    """Only the preferred domain is retried before the hybrid layer steps in.

    If the local domain's own fallback could reach the cloud, the retry would
    happen twice -- once inside the domain and once here -- and the cloud would
    be dispatched before the policy ever chose it.
    """
    dead_local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("local one down"),
    )
    healthy_local = _adapter(
        "compose-model:local-11435",
        provider="local",
        base_url="http://localhost:11435/v1",
    )
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(dead_local, healthy_local, remote)
    shared.register_route(_MODEL_ID, [(dead_local, 1e9), (healthy_local, 1e8), (remote, 1e-9)])
    router = _registry(shared).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="same-domain")

    assert response["_routing"]["endpoint_id"] == "compose-model:local-11435"
    assert response["_routing"]["backend"] == "local"
    # The domain's own loop reported the retry; the hybrid layer added nothing.
    assert [a["endpoint_id"] for a in response["_routing"]["failed_attempts"]] == [_LOCAL_ENDPOINT]
    assert dead_local.chat_calls == 1
    assert healthy_local.chat_calls == 1
    assert remote.chat_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feedback_follows_the_attempt_across_domains() -> None:
    """One request, two backends, and each sample goes to the one that ran it."""
    from routing.endpoint_health import EndpointHealthRegistry

    health = EndpointHealthRegistry()
    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("local down"),
    )
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, remote, health=health)
    shared.register_route(_MODEL_ID, [(local, 1e9), (remote, 1.0)])
    router = _registry(shared).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="feedback-split")

    status = health.snapshot()
    assert _LOCAL_ENDPOINT in status
    assert _CLOUD_ENDPOINT in status
    assert status[_LOCAL_ENDPOINT]["availability"] < 1.0
    assert status[_CLOUD_ENDPOINT]["availability"] == 1.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_chosen_cloud_target_is_not_resampled_away() -> None:
    """Scenario B: a specified target must be obeyed, not re-drawn.

    The route is weighted overwhelmingly toward local, so a target that survives
    is evidence the backend honored the decision instead of running its own
    unconstrained selection.
    """
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, remote)
    shared.register_route(_MODEL_ID, [(local, 1e9), (remote, 1e-9)])
    router = _registry(shared).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    response = await router.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=RoutingRequestOptions(preferred_endpoint_id=_CLOUD_ENDPOINT),
        request_id="targeted-cloud",
    )

    assert response["_routing"]["endpoint_id"] == _CLOUD_ENDPOINT
    assert response["_routing"]["backend"] == "cloud"
    assert response["_routing"]["preference_in_range"] is True
    assert remote.chat_calls == 1
    assert local.chat_calls == 0


@pytest.mark.unit
def test_registry_keeps_the_shared_router_when_no_route_is_cloud() -> None:
    """A local-only model is already served correctly by the shared router."""
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)

    router = _registry(_shared_router(local)).get_router(_MODEL_ID)

    assert not isinstance(router, HybridRouter)
    assert isinstance(router, FixedRouter)


@pytest.mark.unit
def test_policy_splits_global_weights_between_local_and_cloud() -> None:
    """The global weighted draw is split by domain and carries its target."""
    import random

    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = FixedRouter()
    shared.register_route(_MODEL_ID, [(local, 0.9), (remote, 0.1)])
    policy = FixedPolicy(
        compute=shared,
        local_scope={_LOCAL_ENDPOINT},
        cloud_scope={_CLOUD_ENDPOINT},
    )

    random.seed(7)
    decisions = [policy.select_backend(_MODEL_ID, _MESSAGES) for _ in range(50)]

    assert {decision.backend for decision in decisions} == {"local", "cloud"}
    local_picks = [d for d in decisions if d.backend == "local"]
    assert len(local_picks) > len(decisions) - len(local_picks)
    for decision in decisions:
        expected = _LOCAL_ENDPOINT if decision.backend == "local" else _CLOUD_ENDPOINT
        assert decision.target is not None
        assert decision.target.endpoint_id == expected


@pytest.mark.unit
def test_policy_reports_a_provider_target_for_a_caller_pin() -> None:
    """A hard pin names the provider; the backend still enforces pin semantics."""
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    policy = FixedPolicy(
        compute=_shared_router(local, remote),
        local_scope={_LOCAL_ENDPOINT},
        cloud_scope={_CLOUD_ENDPOINT},
    )

    decision = policy.select_backend(
        _MODEL_ID,
        _MESSAGES,
        routing_options=RoutingRequestOptions(pin_provider="zai"),
    )

    assert decision.backend == "cloud"
    assert decision.target is not None
    assert decision.target.provider == "zai"
    assert decision.target.endpoint_id is None
    # A pin suppresses the cross-domain fallback step entirely.
    assert (
        policy.fallback_backends(
            _MODEL_ID,
            _MESSAGES,
            decision,
            routing_options=RoutingRequestOptions(pin_provider="zai"),
        )
        == ()
    )


@pytest.mark.unit
def test_policy_offers_the_other_domain_for_automatic_fallback() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    policy = FixedPolicy(
        compute=_shared_router(local, remote),
        local_scope={_LOCAL_ENDPOINT},
        cloud_scope={_CLOUD_ENDPOINT},
    )
    from routing.decisions import RoutingDecision

    assert policy.fallback_backends(_MODEL_ID, _MESSAGES, RoutingDecision(backend="local")) == (
        "cloud",
    )
    assert policy.fallback_backends(_MODEL_ID, _MESSAGES, RoutingDecision(backend="cloud")) == (
        "local",
    )


@pytest.mark.unit
def test_policy_is_silent_when_only_one_domain_can_serve() -> None:
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    policy = FixedPolicy(
        compute=_shared_router(remote),
        local_scope={_LOCAL_ENDPOINT},
        cloud_scope={_CLOUD_ENDPOINT},
    )
    from routing.decisions import RoutingDecision

    assert policy.fallback_backends(_MODEL_ID, _MESSAGES, RoutingDecision(backend="cloud")) == ()


@pytest.mark.unit
def test_decision_target_rejects_both_identifiers_at_once() -> None:
    from routing.decisions import RoutingTarget

    with pytest.raises(ValueError, match="exactly one"):
        RoutingTarget(provider="zai", endpoint_id=_CLOUD_ENDPOINT)
    with pytest.raises(ValueError, match="exactly one"):
        RoutingTarget()


@pytest.mark.unit
def test_registry_keeps_the_shared_router_when_no_route_is_local() -> None:
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")

    router = _registry(_shared_router(remote)).get_router(_MODEL_ID)

    assert not isinstance(router, HybridRouter)
    assert isinstance(router, FixedRouter)


@pytest.mark.unit
def test_routewise_models_are_not_narrowed_by_the_hybrid_factory() -> None:
    """``router: routewise`` keeps its own router over its own full pool."""
    from routing.routewise.router import RouteWiseRouter

    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, remote)
    from routing.endpoint_health import EndpointHealthRegistry

    registry = ModelRouterRegistry(
        models_config={_MODEL_ID: {"router": "routewise"}},
        default_router_name="fixed",
        dependencies=None,
        shared_fixed_router=shared,
    )
    registry.set_hybrid_router_factory(
        HybridFixedRouterFactory(registry=registry, health_registry=EndpointHealthRegistry())
    )

    router = registry.get_router(_MODEL_ID)

    assert isinstance(router, RouteWiseRouter)
    assert not isinstance(router, HybridRouter)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_composed_router_reaches_both_domains_through_the_policy() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    router = _registry(_shared_router(local, remote)).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    forced_local = RoutingRequestOptions(pin_provider="local")
    response = await router.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=forced_local,
        request_id="compose-local",
    )

    assert response["_routing"]["endpoint_id"] == _LOCAL_ENDPOINT
    assert response["_routing"]["backend"] == "local"
    assert local.chat_calls == 1
    assert remote.chat_calls == 0
