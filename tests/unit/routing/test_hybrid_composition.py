"""The real registry entry point builds the hybrid composition.

These tests do not instantiate ``HybridRouter`` by hand. They build a
``ModelRouterRegistry`` the way ``bootstrap`` does -- shared ``FixedRouter``,
``models.yaml``-shaped config, hybrid factory attached -- and assert on the
routers that entry point actually hands back, so a broken migration fails here
rather than passing beside it.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from routing.backends import FixedCloudBackend, LocalBackend
from routing.decisions import RoutingTarget
from routing.endpoint_health import EndpointHealthRegistry
from routing.endpoints import endpoint_id_for_adapter
from routing.hybrid import HybridRouter
from routing.model_router_registry import ModelRouterRegistry
from routing.policies import FixedPolicy
from routing.prefill_load import PrefillLoadTracker
from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.hybrid_composition import HybridFixedRouterFactory, local_endpoint_scope

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

#: The real load-aware draw, captured before any fixture replaces it: the tests
#: that want the draw pinned do not want it pinned for the tests that assert on
#: the draw itself.
_REAL_SELECT_INDEX = PrefillLoadTracker.select_index

_MODEL_ID = "compose-model"
_MESSAGES = [{"role": "user", "content": "hello"}]
_LOCAL_URL = "http://localhost:11434/v1"
_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-11434"
_CLOUD_ENDPOINT = f"{_MODEL_ID}:zai-api"


class _ComposeAdapter(BaseAdapter):
    """Recording adapter that can fail on demand."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        chat_error: BaseException | None = None,
        stream_chunks: tuple[str, ...] = (),
        stream_error: BaseException | None = None,
    ) -> None:
        super().__init__(config)
        self.chat_error = chat_error
        self.stream_chunks = stream_chunks
        self.stream_error = stream_error
        self.chat_calls = 0
        self.stream_calls = 0
        self.stream_closed = False

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
        self.stream_calls += 1
        try:
            for chunk in self.stream_chunks:
                yield chunk
            if self.stream_error is not None:
                raise self.stream_error
        finally:
            self.stream_closed = True


def _adapter(
    endpoint_id: str,
    *,
    provider: str,
    base_url: str,
    chat_error: BaseException | None = None,
    stream_chunks: tuple[str, ...] = (),
    stream_error: BaseException | None = None,
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
    return _ComposeAdapter(
        config,
        chat_error=chat_error,
        stream_chunks=stream_chunks,
        stream_error=stream_error,
    )


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
async def test_a_failed_spot_moves_to_the_next_candidate_in_the_route(
    _first_candidate_wins: None,
) -> None:
    """A second replica in the same domain is a separate candidate, not a retry.

    The hybrid layer drives the plan itself, so the domain's own fallback loop
    must stay out of it: with both running, the healthy replica would be tried
    once inside the domain and again from the plan, and the cloud would be
    dispatched without the policy ever choosing it.

    The draw is pinned to the first candidate rather than merely weighted toward
    it: at 1e9 against 1e8 the healthy replica is still picked about one run in
    eleven, the request then succeeds on its primary, and the assertions below
    have no ``failed_attempts`` to read.
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


# ----------------------------------------------------------------------
# Regressions the migration must not reintroduce
# ----------------------------------------------------------------------


def _frame(text: str) -> str:
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"


@pytest.fixture
def _first_candidate_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the weighted draw deterministic so the domain under test is the pick."""
    monkeypatch.setattr(PrefillLoadTracker, "select_index", lambda self, *a, **k: 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_failure_after_a_forwarded_chunk_does_not_restart_elsewhere(
    _first_candidate_wins: None,
) -> None:
    """Output already on the wire is committed; the failure propagates.

    ``FixedRouter`` refuses to restart with its ``chunks_yielded`` guard. The
    hybrid layer must refuse it too: falling back here would splice one answer
    from two backends into a single SSE stream and swallow the upstream error, so
    the client would see a 200 with corrupted content and no failure signal.
    """
    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        stream_chunks=(_frame("LOCAL-PARTIAL"),),
        stream_error=ConnectionError("local stream down"),
    )
    remote = _adapter(
        _CLOUD_ENDPOINT,
        provider="zai",
        base_url="https://api.zai.example/v1",
        stream_chunks=(_frame("CLOUD-RESTART"),),
    )
    router = _registry(_shared_router(local, remote)).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    received: list[str] = []
    with pytest.raises(ConnectionError):
        async for chunk in router.stream_chat_completion(
            _MODEL_ID, _MESSAGES, request_id="stream-committed"
        ):
            received.append(chunk)

    delivered = "".join(received)
    assert "LOCAL-PARTIAL" in delivered
    assert remote.stream_calls == 0, "a committed stream must not be restarted"
    assert "CLOUD-RESTART" not in delivered
    assert local.stream_closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_stream_that_fails_before_visible_output_still_falls_back(
    _first_candidate_wins: None,
) -> None:
    """A routing-only frame is not output, so the other domain may still serve.

    Every backend emits a synthetic ``_routing`` frame before anything else, and
    the serving layer drops it before the response leaves the gateway. Treating
    that frame as commitment would strand the request on a backend that never
    produced a visible byte, so the failure below must still hand over.
    """
    routing_only = (
        "data: " + json.dumps({"choices": [], "_routing": {"provider": "local"}}) + "\n\n"
    )
    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        stream_chunks=(routing_only,),
        stream_error=ConnectionError("local failed before any visible output"),
    )
    remote = _adapter(
        _CLOUD_ENDPOINT,
        provider="zai",
        base_url="https://api.zai.example/v1",
        stream_chunks=(_frame("CLOUD-RECOVERY"),),
    )
    router = _registry(_shared_router(local, remote)).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    received = [
        chunk
        async for chunk in router.stream_chat_completion(
            _MODEL_ID, _MESSAGES, request_id="pre-visible-failure"
        )
    ]

    assert remote.stream_calls == 1, "a failure before visible output must still fall back"
    assert "CLOUD-RECOVERY" in "".join(received)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_follows_the_route_order_across_domains(
    _first_candidate_wins: None,
) -> None:
    """The route's candidate order is the order that happens, domains and all.

    With ``L1, cloud, L2`` and a failing ``L1``, the single router answered from
    the cloud. Visiting one domain and then the other answers from ``L2``
    instead -- a different endpoint, with different cost and different capacity
    -- so the plan has to interleave the way the route does.
    """
    dead_local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("L1 down"),
    )
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    second_local = _adapter(
        f"{_MODEL_ID}:local-11500",
        provider="local",
        base_url="http://localhost:11500/v1",
    )
    router = _registry(_shared_router(dead_local, remote, second_local)).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="route-order")

    assert response["_routing"]["backend"] == "cloud"
    assert response["_routing"]["endpoint_id"] == _CLOUD_ENDPOINT
    assert second_local.chat_calls == 0, "the domain's own order displaced the route's"


class _BadRequest(Exception):
    """An upstream 400, the shape adapters raise for a malformed payload."""

    status = 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_request_describing_failure_outranks_a_transport_failure(
    _first_candidate_wins: None,
) -> None:
    """Which failure the caller is told about does not depend on domain order.

    The single router surfaced the 400 a live route returned over the connection
    error a dead one raised, because the 400 describes the request and the
    connection error describes nothing the caller can act on. Reporting the
    transport failure instead would tell the caller to retry a request that can
    never succeed.
    """
    dead_local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("local transport failed"),
    )
    remote = _adapter(
        _CLOUD_ENDPOINT,
        provider="zai",
        base_url="https://api.zai.example/v1",
        chat_error=_BadRequest("invalid input"),
    )
    router = _registry(_shared_router(dead_local, remote)).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    with pytest.raises(_BadRequest):
        await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="surfaced-error")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_failed_endpoint_is_recorded_across_domains(
    _first_candidate_wins: None,
) -> None:
    """The answer names every endpoint that was tried, in the order they were.

    Each attempt is one candidate now, so the history is complete by
    construction: a domain that failed twice contributes two records, not one
    summary of the domain. The records also keep the fields the single router's
    ``failed_attempt`` produced, because the DB log and the fallback diagnostic
    read an attempt's provider from them.
    """
    first_local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("L1 down"),
    )
    second_local = _adapter(
        f"{_MODEL_ID}:local-11500",
        provider="local",
        base_url="http://localhost:11500/v1",
        chat_error=ConnectionError("L2 down"),
    )
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    router = _registry(_shared_router(first_local, second_local, remote)).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="attempt-history")

    assert response["_routing"]["backend"] == "cloud"
    history = response["_routing"]["failed_attempts"]
    assert [attempt["endpoint_id"] for attempt in history] == [
        _LOCAL_ENDPOINT,
        f"{_MODEL_ID}:local-11500",
    ]
    assert [attempt["provider"] for attempt in history] == ["local", "local"]
    assert [attempt["backend"] for attempt in history] == ["local", "local"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_policy_draw_uses_the_request_prefill_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The draw that names the preferred domain is prefill-aware.

    A local replica already carrying a large prefill must be steered away from,
    which is what the estimate is for. Dropping it made every request look like a
    zero-token one, so a second long request landed on the replica the first was
    still prefilling.
    """
    monkeypatch.setattr(PrefillLoadTracker, "select_index", _REAL_SELECT_INDEX)
    monkeypatch.setattr("routing.routers.random.random", lambda: 0.0)

    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, remote)
    shared._prefill_load = PrefillLoadTracker(elephant_tokens=10, elephant_limit=1)
    router = _registry(shared).get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    existing = shared.prefill_load.acquire(_LOCAL_ENDPOINT, 100)
    try:
        response = await router.chat_completion(
            _MODEL_ID,
            [{"role": "user", "content": "word " * 100}],
            request_id="prefill",
        )
    finally:
        shared.prefill_load.release(existing)

    assert response["_routing"]["endpoint_id"] == _CLOUD_ENDPOINT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_policy_preference_does_not_bypass_the_half_open_probe(
    _first_candidate_wins: None,
) -> None:
    """A preferred target is an automatic pick, not a caller pin.

    The policy names an endpoint on every request, so exempting that path from
    ``begin_dispatch`` would let every concurrent request into an endpoint whose
    circuit is recovering -- the stampede the single-probe rule exists to
    prevent. Only ``pin_provider`` overrides admission.
    """
    import time as _time

    from routing.endpoint_health import _CircuitState

    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, remote)
    router = _registry(shared).get_router(_MODEL_ID)
    health = shared.endpoint_health_registry
    health.ensure(_LOCAL_ENDPOINT)
    circuit = health._circuits[_LOCAL_ENDPOINT]
    circuit.state = _CircuitState.OPEN
    circuit.last_opened = _time.perf_counter() - circuit.cooldown_seconds - 1.0
    assert health.allow_request(_LOCAL_ENDPOINT)

    entered = asyncio.Event()
    finish = asyncio.Event()

    async def blocking(messages: list[dict[str, Any]], **params: Any) -> dict[str, Any]:
        entered.set()
        await finish.wait()
        return local.format_response(content="ok", model=_MODEL_ID)

    local.chat_completion = blocking  # type: ignore[method-assign]
    task = asyncio.create_task(
        router.chat_completion(_MODEL_ID, _MESSAGES, request_id="probe-holder")
    )
    try:
        await asyncio.wait_for(entered.wait(), 1.0)
        assert not health.allow_request(_LOCAL_ENDPOINT), (
            "the recovering endpoint admitted a second request while a probe is in flight"
        )
    finally:
        finish.set()
        await task


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_refresh_moves_dispatch_off_a_removed_cloud_endpoint() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    removed = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    replacement = _adapter(
        f"{_MODEL_ID}:replacement-api",
        provider="replacement",
        base_url="https://api.replacement.example/v1",
    )
    shared = _shared_router(local, removed)
    registry = _registry(shared)
    router = registry.get_router(_MODEL_ID)

    shared.register_route(_MODEL_ID, [(local, 0.0), (replacement, 1.0)])
    registry.refresh_route_tables()

    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="refresh-removed")

    assert removed.chat_calls == 0, "a deleted cloud endpoint is still being dispatched to"
    assert response["_routing"]["endpoint_id"] == replacement.config.endpoint_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_refresh_moves_a_new_local_endpoint_into_the_local_domain() -> None:
    """The split follows the route table, not the moment the router was built."""
    first = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    added = _adapter(
        f"{_MODEL_ID}:local-11500",
        provider="local",
        base_url="http://localhost:11500/v1",
    )
    shared = _shared_router(first, remote)
    registry = _registry(shared)
    router = registry.get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)
    local_backend = router.backend("local")
    assert isinstance(local_backend, LocalBackend)
    assert local_backend.endpoint_scope == frozenset({_LOCAL_ENDPOINT})

    shared.register_route(_MODEL_ID, [(added, 1.0), (remote, 0.0)])
    registry.refresh_route_tables()

    assert local_backend.endpoint_scope == frozenset({added.config.endpoint_id})
    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="refresh-added")
    assert response["_routing"]["backend"] == "local"
    assert response["_routing"]["endpoint_id"] == added.config.endpoint_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alias_requests_keep_the_canonical_domain_split(
    _first_candidate_wins: None,
) -> None:
    """An alias resolves through the router that knows it, in both domains.

    The cloud backend used to be built over a *copy* of the model's routes, and
    the copy stamped each route key as its own canonical id. An alias request
    then looked like a model the cloud domain did not serve: cross-domain
    fallback was filtered out entirely, the cloud scope stopped being injected,
    and cloud observations found no owner.
    """
    alias = "compose-alias"
    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("local down"),
    )
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = FixedRouter(health_registry=EndpointHealthRegistry())
    shared.register_route(_MODEL_ID, [(local, 1.0), (remote, 1.0)], aliases=[alias])
    registry = ModelRouterRegistry(
        models_config={_MODEL_ID: {"router": "fixed"}},
        default_router_name="fixed",
        alias_to_model={alias: _MODEL_ID},
        shared_fixed_router=shared,
    )
    registry.set_hybrid_router_factory(
        HybridFixedRouterFactory(registry=registry, health_registry=shared.endpoint_health_registry)
    )
    router = registry.get_router(alias)
    assert isinstance(router, HybridRouter)

    response = await router.chat_completion(alias, _MESSAGES, request_id="alias-1")

    assert response["_routing"]["backend"] == "cloud"
    assert response["_routing"]["endpoint_id"] == _CLOUD_ENDPOINT
    assert local.chat_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cloud_domain_keeps_the_registered_weight_override_baseline() -> None:
    """A partial admin override applies to the weights the operator registered.

    The cloud backend used to be seeded from the shared route's *normalized*
    weights, which become the baseline an override is applied to, so one
    overridden endpoint silently re-weighted every sibling.
    """
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    sibling = _adapter(
        f"{_MODEL_ID}:chutes-api",
        provider="chutes",
        base_url="https://api.chutes.example/v1",
    )
    shared = FixedRouter(health_registry=EndpointHealthRegistry())
    shared.register_route(_MODEL_ID, [(local, 10.0), (remote, 5.0), (sibling, 1.0)])
    shared.weight_override_resolver = _OverrideResolver({_CLOUD_ENDPOINT: 5.0})
    registry = _registry(shared)
    router = registry.get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    cloud = router.backend("cloud")
    assert isinstance(cloud, FixedCloudBackend)
    effective = {
        endpoint_id_for_adapter(adapter): weight
        for adapter, weight in cloud.router._get_effective_adapters(
            _MODEL_ID, cloud.router.routes[_MODEL_ID]
        )
    }

    # zai is overridden to 5.0 and keeps its registered sibling at 1.0; against
    # the normalized baseline the same override would have left the sibling at
    # 1/16 and zai at 5/16, i.e. a 9:1 split instead of 5:1.
    assert effective[_CLOUD_ENDPOINT] == 5.0
    assert effective[f"{_MODEL_ID}:chutes-api"] == 1.0


class _OverrideResolver:
    """Minimal partial weight-override resolver, shaped like the production one."""

    def __init__(self, overrides: dict[str, float]) -> None:
        self._overrides = overrides

    def get_snapshot_for_model(self, model_id: str) -> dict[str, float]:
        return dict(self._overrides)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weight_override_resolver_attached_after_build_still_applies() -> None:
    """Bootstrap attaches the resolver after the registry is built.

    The cloud backend must read the shared router's attributes live. A copy taken
    at construction would freeze ``weight_override_resolver=None`` and the whole
    cloud domain would quietly ignore every admin weight override.
    """
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, remote)
    registry = _registry(shared)
    router = registry.get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    shared.weight_override_resolver = _OverrideResolver({_CLOUD_ENDPOINT: 7.0})

    cloud = router.backend("cloud")
    assert isinstance(cloud, FixedCloudBackend)
    effective = {
        endpoint_id_for_adapter(adapter): weight
        for adapter, weight in cloud.router._get_effective_adapters(
            _MODEL_ID, cloud.router.routes[_MODEL_ID]
        )
    }
    assert effective[_CLOUD_ENDPOINT] == 7.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_routewise_can_serve_as_the_cloud_domain_inside_the_composition(
    _first_candidate_wins: None,
) -> None:
    """RouteWise is a cloud *implementation*, reachable from the composition root.

    The factory used to hard-code ``FixedCloudBackend``, so the role the hybrid
    router is typed against had no production implementation at all. This drives
    RouteWise through the cloud-algorithm seam and asserts the domain bound still
    holds: a local-only failure hands over to RouteWise, and RouteWise can only
    reach cloud endpoints.
    """
    from routing.backends import RouteWiseCloudBackend
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.router import RouteWiseRouter

    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("local down"),
    )
    second_local = _adapter(
        f"{_MODEL_ID}:local-11500",
        provider="local",
        base_url="http://localhost:11500/v1",
        chat_error=ConnectionError("second local down"),
    )
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, second_local, remote)

    def build_routewise_cloud(router: FixedRouter, model_id: str, scope: Any) -> Any:
        return RouteWiseCloudBackend(
            RouteWiseRouter(
                config=RouteWiseConfig(budget_alpha=0.0, fallback_mode="policy", random_seed=0)
            ),
            table=router,
            endpoint_scope=frozenset(scope() if callable(scope) else scope),
            model_scope={model_id},
        )

    registry = ModelRouterRegistry(
        models_config={_MODEL_ID: {"router": "fixed"}},
        default_router_name="fixed",
        shared_fixed_router=shared,
    )
    registry.set_hybrid_router_factory(
        HybridFixedRouterFactory(
            registry=registry,
            health_registry=shared.endpoint_health_registry,
            cloud_backend=build_routewise_cloud,
        )
    )
    router = registry.get_router(_MODEL_ID)
    assert isinstance(router, HybridRouter)

    cloud = router.backend("cloud")
    assert isinstance(cloud, RouteWiseCloudBackend)
    assert cloud.allowed_endpoints() == frozenset({_CLOUD_ENDPOINT}), (
        "the RouteWise cloud domain must not reach the local replicas"
    )

    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="routewise-cloud")

    assert response["_routing"]["backend"] == "cloud"
    assert local.chat_calls == 1
    assert second_local.chat_calls == 1
    assert remote.chat_calls == 1


@pytest.mark.unit
def test_a_declared_cloud_scope_gates_the_policy() -> None:
    """An explicitly empty cloud range means the policy never offers cloud."""
    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, remote)
    policy = FixedPolicy(
        compute=shared,
        local_scope=frozenset({_LOCAL_ENDPOINT}),
        cloud_scope=frozenset(),
    )

    assert not policy._domain_serves(
        policy._cloud_backend, _MODEL_ID, frozenset({_LOCAL_ENDPOINT}), frozenset()
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_label_cloud_scope_still_attributes_cloud_feedback() -> None:
    """A label scope must claim the endpoints that provider serves.

    Dispatch already matched a provider label, but ownership and target
    resolution compared the observation's endpoint id against the label
    literally, so declaring ``{"zai"}`` dropped every cloud observation.
    """
    from routing.routers import RoutingObservation

    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    remote = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, remote)
    cloud = FixedCloudBackend(shared, endpoint_scope=frozenset({"zai"}), model_scope={_MODEL_ID})

    assert cloud.dispatch_scope(_MODEL_ID) == frozenset({"zai"})
    assert cloud.resolve_target(RoutingTarget(endpoint_id=_CLOUD_ENDPOINT), _MODEL_ID) == (
        _CLOUD_ENDPOINT
    )
    assert cloud.owns_observation(
        RoutingObservation(
            model_id=_MODEL_ID,
            endpoint_id=_CLOUD_ENDPOINT,
            ttft_ms=1.0,
            total_latency_ms=2.0,
            token_count=1,
            success=True,
        )
    )
    assert not cloud.owns_observation(
        RoutingObservation(
            model_id=_MODEL_ID,
            endpoint_id=_LOCAL_ENDPOINT,
            ttft_ms=1.0,
            total_latency_ms=2.0,
            token_count=1,
            success=True,
        )
    )
