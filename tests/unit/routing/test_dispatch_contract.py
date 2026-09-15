"""The dispatch boundary: what a router may ask an executor to do, and what it may not.

Two instructions exist and they are not interchangeable. ``ExecuteEndpoint``
binds one endpoint and ends the recursion; ``DelegatePool`` grants selection
inside a named pool. A leaf takes only the first, a pool only the second, and a
mismatch is refused before any upstream I/O -- a composition error, never an
upstream fault.

A leaf binds the adapter itself. It runs that adapter and nothing else: no
selection, no second copy of the health or prefill accounting, no reservation of
its own. The router that chose the endpoint keeps all of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from routing.backends import (
    FixedCloudBackend,
    LeafBackend,
    LocalBackend,
    RouteWiseCloudBackend,
    TreeBackend,
)
from routing.decisions import FallbackAttempt, RoutingDecision, RoutingTarget
from routing.dispatch import (
    DelegatePool,
    DispatchMismatchError,
    EndpointBinding,
    ExecuteEndpoint,
    backend_pool_id,
    binding_for_adapter,
    check_dispatch,
    dispatch_for_attempt,
)
from routing.endpoint_health import EndpointHealthRegistry
from routing.endpoints import endpoint_id_for_adapter
from routing.hybrid import HybridRouter
from routing.protocols import RoutingRequestOptions
from routing.routers import AllCircuitsOpenError, FixedRouter, TargetUnavailableError
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "compose-model"
_MESSAGES = [{"role": "user", "content": "hello"}]
_LOCAL_URL = "http://localhost:11434/v1"
_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-11434"
_SECOND_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-11500"
_CLOUD_ENDPOINT = f"{_MODEL_ID}:zai-api"
_CHEAP_ENDPOINT = f"{_MODEL_ID}:cloud-b"


class _RecordingAdapter(BaseAdapter):
    """Adapter double that counts dispatches, can fail, and records parameters."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        chat_error: BaseException | None = None,
        stream_error: BaseException | None = None,
    ) -> None:
        super().__init__(config)
        self.chat_error = chat_error
        self.stream_error = stream_error
        self.chat_calls = 0
        self.stream_calls = 0
        self.seen_params: list[dict[str, Any]] = []
        self.stream_closed = False

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        self.chat_calls += 1
        self.seen_params.append(dict(params))
        if self.chat_error is not None:
            raise self.chat_error
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        self.stream_calls += 1
        self.seen_params.append(dict(params))
        try:
            if self.stream_error is not None:
                raise self.stream_error
            yield 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
        finally:
            self.stream_closed = True


def _adapter(
    endpoint_id: str,
    *,
    provider: str,
    base_url: str,
    chat_error: BaseException | None = None,
    stream_error: BaseException | None = None,
) -> _RecordingAdapter:
    return _RecordingAdapter(
        ModelConfig(
            id=_MODEL_ID,
            name=_MODEL_ID,
            provider=provider,
            base_url=base_url,
            endpoint_id=endpoint_id,
            pricing={"prompt": "1", "completion": "1"},
            input_modalities=["text"],
        ),
        chat_error=chat_error,
        stream_error=stream_error,
    )


def _shared_router(*adapters: _RecordingAdapter) -> FixedRouter:
    router = FixedRouter(health_registry=EndpointHealthRegistry())
    router.register_route(_MODEL_ID, [(adapter, 1.0) for adapter in adapters])
    return router


def _leaf(adapter: _RecordingAdapter, **overrides: Any) -> LeafBackend:
    binding = binding_for_adapter(
        adapter,
        model_id=_MODEL_ID,
        pool_id=overrides.pop("pool_id", None),
        generation=overrides.pop("generation", None),
    )
    return LeafBackend.for_binding(binding, **overrides)


# ---------------------------------------------------------------------------
# The compatibility mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_exact_attempt_maps_to_an_endpoint_binding() -> None:
    adapter = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://a.example/v1")
    binding = binding_for_adapter(adapter, model_id=_MODEL_ID, pool_id="cloud-model-b")

    instruction = dispatch_for_attempt(
        pool_id="cloud-model-b",
        model_id=_MODEL_ID,
        binding=binding,
        exact=True,
    )

    assert isinstance(instruction, ExecuteEndpoint)
    assert instruction.binding.endpoint_id == _CLOUD_ENDPOINT
    assert instruction.binding.adapter is adapter, "the binding must carry the adapter"
    assert instruction.binding.pool_id == "cloud-model-b"


@pytest.mark.unit
def test_an_exact_attempt_without_a_binding_is_refused() -> None:
    """A hard target that names nothing cannot be expressed as a delegation."""
    with pytest.raises(DispatchMismatchError, match="requires a resolved endpoint binding"):
        dispatch_for_attempt(
            pool_id="cloud",
            model_id=_MODEL_ID,
            binding=None,
            exact=True,
        )


@pytest.mark.unit
def test_a_delegating_attempt_maps_to_a_pool_delegation() -> None:
    instruction = dispatch_for_attempt(
        pool_id="cloud",
        model_id=_MODEL_ID,
        binding=None,
        exact=False,
    )

    assert instruction == DelegatePool(pool_id="cloud")


@pytest.mark.unit
def test_a_preference_stays_a_delegation() -> None:
    """Mapping a preference onto a hard target is the mistake this guards.

    An endpoint the caller merely prefers may still be replaced by the pool's own
    selection. Only an exact attempt keeps it.
    """
    adapter = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://a.example/v1")

    instruction = dispatch_for_attempt(
        pool_id="cloud",
        model_id=_MODEL_ID,
        binding=binding_for_adapter(adapter, model_id=_MODEL_ID),
        exact=False,
    )

    assert isinstance(instruction, DelegatePool)


@pytest.mark.unit
def test_a_binding_requires_the_adapter_that_runs_the_endpoint() -> None:
    with pytest.raises(ValueError, match="requires the adapter"):
        EndpointBinding(endpoint_id=_CLOUD_ENDPOINT, model_id=_MODEL_ID, adapter=None)


# ---------------------------------------------------------------------------
# The leaf: one adapter, executed as it is
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_leaf_exposes_the_adapters_own_config() -> None:
    adapter = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)

    leaf = _leaf(adapter)

    assert leaf.config is adapter.config, "a copied config would drift from the adapter"
    assert leaf.endpoint_id == _LOCAL_ENDPOINT
    assert leaf.adapter is adapter


@pytest.mark.unit
def test_a_binding_carries_the_generation_for_diagnostics() -> None:
    adapter = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)

    leaf = _leaf(adapter, pool_id="owned", generation=7)

    assert leaf.pool_id == "owned"
    assert leaf.generation == 7


@pytest.mark.unit
def test_a_leaf_refuses_a_composite_executor() -> None:
    """An executor that fans out across endpoints cannot be bound to one."""

    class _Composite(_RecordingAdapter):
        reports_leg_outcomes = True

    composite = _Composite(
        ModelConfig(
            id=_MODEL_ID,
            name=_MODEL_ID,
            provider="hedged",
            base_url="https://a.example/v1",
            endpoint_id=_CLOUD_ENDPOINT,
            pricing={"prompt": "1", "completion": "1"},
            input_modalities=["text"],
        )
    )

    with pytest.raises(DispatchMismatchError, match="across endpoints"):
        _leaf(composite)


@pytest.mark.unit
def test_a_leaf_refuses_a_delegation_and_a_foreign_endpoint() -> None:
    adapter = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    leaf = _leaf(adapter)

    with pytest.raises(DispatchMismatchError, match="has no pool to delegate to"):
        check_dispatch(leaf, DelegatePool(pool_id="local"), _MODEL_ID)
    with pytest.raises(DispatchMismatchError, match="was asked to execute"):
        check_dispatch(
            leaf,
            ExecuteEndpoint(
                EndpointBinding(
                    endpoint_id=_SECOND_LOCAL_ENDPOINT, model_id=_MODEL_ID, adapter=adapter
                )
            ),
            _MODEL_ID,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_leaf_passes_the_response_and_the_parameters_through() -> None:
    adapter = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    leaf = _leaf(adapter)

    response = await leaf.chat_completion(_MESSAGES, temperature=0.5, request_id="leaf")

    assert adapter.chat_calls == 1
    assert adapter.seen_params == [{"temperature": 0.5, "request_id": "leaf"}]
    assert response["model"] == _MODEL_ID
    # The leaf annotates nothing: ``_routing`` is written by the router that
    # dispatched, and a second copy here would be a second source of truth.
    assert "_routing" not in response


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_leaf_returns_the_adapters_own_iterator() -> None:
    """A plain ``def``: the caller drives the upstream stream directly."""
    adapter = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    leaf = _leaf(adapter)

    stream = leaf.stream_chat_completion(_MESSAGES, temperature=0.25)

    assert hasattr(stream, "__anext__"), "a leaf must hand back an iterator, not a coroutine"
    chunks = [chunk async for chunk in stream]
    assert adapter.stream_calls == 1
    assert adapter.seen_params == [{"temperature": 0.25}]
    assert '"content"' in "".join(chunks)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_leaf_propagates_the_upstream_error_unchanged() -> None:
    adapter = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("upstream down"),
    )
    leaf = _leaf(adapter)

    with pytest.raises(ConnectionError, match="upstream down"):
        await leaf.chat_completion(_MESSAGES)


# ---------------------------------------------------------------------------
# Pools: delegation, range, and the label form of a range
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_existing_wrappers_are_pools() -> None:
    shared = _shared_router(
        _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL),
        _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1"),
    )
    local = LocalBackend(shared, endpoint_scope={_LOCAL_ENDPOINT}, model_scope={_MODEL_ID})
    cloud = FixedCloudBackend(shared, endpoint_scope={_CLOUD_ENDPOINT}, model_scope={_MODEL_ID})

    assert isinstance(local, TreeBackend)
    assert isinstance(cloud, TreeBackend)
    assert backend_pool_id(local) == "local"
    assert local.pool_id == "local"
    assert cloud.pool_id == "cloud"


@pytest.mark.unit
def test_a_pool_refuses_a_delegation_to_another_pool() -> None:
    local = LocalBackend(
        _shared_router(_adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)),
        endpoint_scope={_LOCAL_ENDPOINT},
        model_scope={_MODEL_ID},
    )

    check_dispatch(local, DelegatePool(pool_id="local"), _MODEL_ID)
    with pytest.raises(DispatchMismatchError, match="was asked to serve pool"):
        check_dispatch(local, DelegatePool(pool_id="cloud"), _MODEL_ID)


@pytest.mark.unit
def test_a_label_range_accepts_an_exact_binding_it_covers() -> None:
    """A pool declared by provider label must not reject its own endpoints.

    Comparing the binding's endpoint id against raw label entries reads a
    legitimate range as disjoint, which silently breaks the fallback that named
    it.
    """
    cloud = _adapter(_CLOUD_ENDPOINT, provider="cloud-provider", base_url="https://a.example/v1")
    pool = FixedCloudBackend(
        _shared_router(cloud),
        endpoint_scope={"cloud-provider"},
        model_scope={_MODEL_ID},
    )
    binding = pool.binding_for(_MODEL_ID, _CLOUD_ENDPOINT)

    assert binding is not None and binding.adapter is cloud
    check_dispatch(pool, ExecuteEndpoint(binding), _MODEL_ID)


@pytest.mark.unit
def test_a_pool_refuses_a_binding_outside_its_range() -> None:
    local = LocalBackend(
        _shared_router(
            _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL),
            _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1"),
        ),
        endpoint_scope={_LOCAL_ENDPOINT},
        model_scope={_MODEL_ID},
    )
    outside = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")

    with pytest.raises(DispatchMismatchError, match="may dispatch inside"):
        check_dispatch(
            local,
            ExecuteEndpoint(binding_for_adapter(outside, model_id=_MODEL_ID)),
            _MODEL_ID,
        )


@pytest.mark.unit
def test_a_pool_binding_is_read_from_its_own_route_table() -> None:
    """A pool can only bind an endpoint it actually serves."""
    local = LocalBackend(
        _shared_router(_adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)),
        endpoint_scope={_LOCAL_ENDPOINT},
        model_scope={_MODEL_ID},
    )

    assert local.binding_for(_MODEL_ID, _LOCAL_ENDPOINT) is not None
    assert local.binding_for(_MODEL_ID, _CLOUD_ENDPOINT) is None


@pytest.mark.unit
def test_a_caller_scope_can_narrow_a_pool_but_never_widen_it() -> None:
    first = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    second = _adapter(_SECOND_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    local = LocalBackend(
        _shared_router(first, second),
        endpoint_scope={_LOCAL_ENDPOINT, _SECOND_LOCAL_ENDPOINT},
        model_scope={_MODEL_ID},
    )

    narrowed = local._scoped_options(
        RoutingRequestOptions(endpoint_scope=frozenset({_SECOND_LOCAL_ENDPOINT})), _MODEL_ID
    )
    assert narrowed is not None
    assert narrowed.endpoint_scope == frozenset({_SECOND_LOCAL_ENDPOINT})

    widened = local._scoped_options(
        RoutingRequestOptions(endpoint_scope=frozenset({_CLOUD_ENDPOINT}) | {_LOCAL_ENDPOINT}),
        _MODEL_ID,
    )
    assert widened is not None
    assert widened.endpoint_scope == frozenset({_LOCAL_ENDPOINT})


@pytest.mark.unit
def test_a_pool_refuses_a_range_that_excludes_everything_it_serves() -> None:
    local = LocalBackend(
        _shared_router(_adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)),
        endpoint_scope={_LOCAL_ENDPOINT},
        model_scope={_MODEL_ID},
    )

    with pytest.raises(DispatchMismatchError, match="was restricted to"):
        local._scoped_options(
            RoutingRequestOptions(endpoint_scope=frozenset({_CLOUD_ENDPOINT})), _MODEL_ID
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_pool_delegation_lets_its_router_choose_inside_the_range() -> None:
    first = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    second = _adapter(_SECOND_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    outside = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    local = LocalBackend(
        _shared_router(first, second, outside),
        endpoint_scope={_LOCAL_ENDPOINT, _SECOND_LOCAL_ENDPOINT},
        model_scope={_MODEL_ID},
    )

    response = await local.chat_completion(_MODEL_ID, _MESSAGES)

    assert endpoint_id_for_adapter(outside) != response["_routing"]["endpoint_id"]
    assert outside.chat_calls == 0, "the delegation reached outside the pool"
    assert first.chat_calls + second.chat_calls == 1


def _routewise_router(table: FixedRouter) -> Any:
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.router import RouteWiseRouter

    return RouteWiseRouter(
        route_table=table,
        config=RouteWiseConfig(budget_alpha=0.0, random_seed=0, routewise_probe_enabled=False),
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_narrowed_caller_range_reaches_a_routewise_pool(streaming: bool) -> None:
    """A pool's own router consumes the narrowed range, not just the wrapper.

    Computing an intersection the sub-router then ignores would let a request be
    served by an endpoint the caller excluded. The decision is made over the
    candidate set, so the range has to reach it.
    """
    expensive = _adapter(_CLOUD_ENDPOINT, provider="expensive", base_url="https://a.example/v1")
    cheap = _adapter(_CHEAP_ENDPOINT, provider="cheap", base_url="https://b.example/v1")
    table = _shared_router(expensive, cheap)
    pool = RouteWiseCloudBackend(
        _routewise_router(table),
        table=table,
        endpoint_scope={_CLOUD_ENDPOINT, _CHEAP_ENDPOINT},
        model_scope={_MODEL_ID},
    )
    options = RoutingRequestOptions(endpoint_scope=frozenset({_CLOUD_ENDPOINT}))

    if streaming:
        chunks = [
            chunk
            async for chunk in pool.stream_chat_completion(
                _MODEL_ID, _MESSAGES, routing_options=options
            )
        ]
        assert chunks
    else:
        await pool.chat_completion(_MODEL_ID, _MESSAGES, routing_options=options)

    assert expensive.chat_calls + expensive.stream_calls == 1
    assert cheap.chat_calls + cheap.stream_calls == 0, (
        "the sub-router served an endpoint the caller excluded"
    )


# ---------------------------------------------------------------------------
# Compositions
# ---------------------------------------------------------------------------


class _DelegateToCloudPolicy:
    """Policy that hands the request to the cloud pool and nothing else."""

    def select_backend(self, *args: Any, **kwargs: Any) -> RoutingDecision:
        return RoutingDecision(backend="cloud")

    def fallback_backends(self, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        return ()


class _DelegateToLocalPolicy:
    """Policy that hands the request to the local pool and nothing else."""

    def select_backend(self, *args: Any, **kwargs: Any) -> RoutingDecision:
        return RoutingDecision(backend="local")

    def fallback_backends(self, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        return ()


class _PlannedPolicy:
    """Policy that names the candidate order itself, as ``FixedPolicy`` does."""

    def __init__(self, *, primary: str, primary_endpoint: str, fallback_endpoint: str) -> None:
        self._primary = primary
        self._primary_endpoint = primary_endpoint
        self._fallback_endpoint = fallback_endpoint

    def select_backend(self, *args: Any, **kwargs: Any) -> RoutingDecision:
        return RoutingDecision(
            backend=self._primary,
            target=RoutingTarget(endpoint_id=self._primary_endpoint),
        )

    def fallback_backends(self, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        return ()

    def fallback_attempts(self, *args: Any, **kwargs: Any) -> tuple[FallbackAttempt, ...]:
        return (
            FallbackAttempt(
                backend="cloud",
                target=RoutingTarget(endpoint_id=self._fallback_endpoint),
            ),
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_the_caller_range_survives_the_composition_to_a_pool(streaming: bool) -> None:
    """The hybrid layer forwards a caller's grant, it does not replace it.

    Replacing it with the pool's own range hands the pool back a wider grant than
    the caller made, and the pool's intersection check cannot recover a
    restriction it never received.
    """
    local = _adapter(_LOCAL_ENDPOINT, provider="owned", base_url=_LOCAL_URL)
    allowed = _adapter(_CLOUD_ENDPOINT, provider="expensive", base_url="https://a.example/v1")
    cheaper = _adapter(_CHEAP_ENDPOINT, provider="cheap", base_url="https://b.example/v1")
    shared = _shared_router(local, allowed, cheaper)
    router = HybridRouter(
        policy=_DelegateToCloudPolicy(),
        local=LocalBackend(shared, endpoint_scope={_LOCAL_ENDPOINT}, model_scope={_MODEL_ID}),
        cloud=RouteWiseCloudBackend(
            _routewise_router(shared),
            table=shared,
            endpoint_scope={_CLOUD_ENDPOINT, _CHEAP_ENDPOINT},
            model_scope={_MODEL_ID},
        ),
    )
    options = RoutingRequestOptions(endpoint_scope=frozenset({_CLOUD_ENDPOINT}))

    if streaming:
        _ = [
            chunk
            async for chunk in router.stream_chat_completion(
                _MODEL_ID, _MESSAGES, routing_options=options
            )
        ]
    else:
        await router.chat_completion(_MODEL_ID, _MESSAGES, routing_options=options)

    assert allowed.chat_calls + allowed.stream_calls == 1
    assert cheaper.chat_calls + cheaper.stream_calls == 0, (
        "the composition widened the range the caller granted"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_plan_that_was_never_dispatched_still_reports_why() -> None:
    """Dropping the failed sample must not drop the reason.

    With every candidate refused, the caller is told that nothing was admissible
    -- the refusal itself -- rather than a generic "every backend failed".
    """
    import time as _time

    from routing.endpoint_health import _CircuitState

    local = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local, cloud)
    health = shared.endpoint_health_registry
    health.ensure(_CLOUD_ENDPOINT)
    circuit = health._circuits[_CLOUD_ENDPOINT]
    circuit.state = _CircuitState.OPEN
    circuit.last_opened = _time.perf_counter()
    router = HybridRouter(
        policy=_DelegateToCloudPolicy(),
        local=LocalBackend(shared, endpoint_scope={_LOCAL_ENDPOINT}, model_scope={_MODEL_ID}),
        cloud=FixedCloudBackend(shared, endpoint_scope={_CLOUD_ENDPOINT}, model_scope={_MODEL_ID}),
    )

    with pytest.raises(AllCircuitsOpenError) as raised:
        await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="all-refused")

    assert local.chat_calls == 0
    assert cloud.chat_calls == 0
    # The refusal is the reason, not an attempt: nothing was sent, so the error
    # must not carry a failure record for an endpoint that was never contacted.
    assert not getattr(raised.value, "_routing", {}).get("failed_attempts")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_busy_pool_delegation_is_not_a_provider_failure() -> None:
    """A pool with no capacity left has not failed.

    Whether the attempt was an exact binding or a delegation does not change
    whether an upstream was contacted, and only that decides whether there is a
    provider fault to record.
    """
    local = _adapter(_LOCAL_ENDPOINT, provider="owned", base_url=_LOCAL_URL)
    local.config.provider_type = "concurrency"
    local.config.concurrency_pool = "one-slot"
    local.config.concurrency = {"limit": 1}
    cloud = _adapter(_CLOUD_ENDPOINT, provider="metered", base_url="https://a.example/v1")
    table = _shared_router(local, cloud)
    local_router = _routewise_router(table)
    pool = local_router.concurrency_pools["one-slot"]
    assert pool.try_acquire()
    router = HybridRouter(
        policy=_PlannedPolicy(
            primary="local",
            primary_endpoint=_LOCAL_ENDPOINT,
            fallback_endpoint=_CLOUD_ENDPOINT,
        ),
        local=LocalBackend(local_router, endpoint_scope={_LOCAL_ENDPOINT}, model_scope={_MODEL_ID}),
        cloud=FixedCloudBackend(table, endpoint_scope={_CLOUD_ENDPOINT}, model_scope={_MODEL_ID}),
    )
    try:
        response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="busy-pool")

        assert local.chat_calls == 0
        assert cloud.chat_calls == 1
        assert pool.active == 1, "another request's occupied slot was released"
        assert not response["_routing"].get("failed_attempts"), (
            "nothing was sent, but attempts were recorded"
        )
    finally:
        pool.release()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_composition_error_is_not_recorded_as_a_failed_attempt() -> None:
    """An empty pool is a composition mistake, not an upstream failure to retry."""
    local_adapter = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    cloud_adapter = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local_adapter, cloud_adapter)
    router = HybridRouter(
        policy=_DelegateToLocalPolicy(),
        local=LocalBackend(shared, endpoint_scope=set(), model_scope={_MODEL_ID}, name="local"),
        cloud=FixedCloudBackend(shared, endpoint_scope={_CLOUD_ENDPOINT}, model_scope={_MODEL_ID}),
    )

    with pytest.raises(DispatchMismatchError):
        await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="empty-pool")

    assert local_adapter.chat_calls == 0
    assert cloud_adapter.chat_calls == 0, "the request fell through to another backend"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_composition_error_propagates_from_the_streaming_path_too() -> None:
    """Both request paths dispose of a composition error the same way."""
    local_adapter = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    cloud_adapter = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local_adapter, cloud_adapter)
    router = HybridRouter(
        policy=_DelegateToLocalPolicy(),
        local=LocalBackend(shared, endpoint_scope=set(), model_scope={_MODEL_ID}, name="local"),
        cloud=FixedCloudBackend(shared, endpoint_scope={_CLOUD_ENDPOINT}, model_scope={_MODEL_ID}),
    )

    with pytest.raises(DispatchMismatchError):
        _ = [
            chunk
            async for chunk in router.stream_chat_completion(
                _MODEL_ID, _MESSAGES, request_id="empty-pool-stream"
            )
        ]

    assert local_adapter.stream_calls == 0
    assert cloud_adapter.stream_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_exact_target_still_reaches_only_the_bound_endpoint() -> None:
    """The behaviour the binding exists for, through a real router.

    A dispatch that requires one endpoint must reach that endpoint and no other,
    even when a sibling is healthier or cheaper.
    """
    wanted = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    sibling = _adapter(_SECOND_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    router = _shared_router(wanted, sibling)

    response = await router.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=RoutingRequestOptions(
            preferred_endpoint_id=_SECOND_LOCAL_ENDPOINT,
            require_target=True,
            allow_fallback=False,
        ),
    )

    assert response["_routing"]["endpoint_id"] == _SECOND_LOCAL_ENDPOINT
    assert sibling.chat_calls == 1
    assert wanted.chat_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unavailable_exact_target_does_not_substitute_another() -> None:
    """A required endpoint that cannot be admitted reports that, not a sibling."""
    import time as _time

    from routing.endpoint_health import _CircuitState

    wanted = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    sibling = _adapter(_SECOND_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    router = _shared_router(wanted, sibling)
    health = router.endpoint_health_registry
    health.ensure(_LOCAL_ENDPOINT)
    circuit = health._circuits[_LOCAL_ENDPOINT]
    circuit.state = _CircuitState.OPEN
    circuit.last_opened = _time.perf_counter()

    with pytest.raises(TargetUnavailableError):
        await router.chat_completion(
            _MODEL_ID,
            _MESSAGES,
            routing_options=RoutingRequestOptions(
                preferred_endpoint_id=_LOCAL_ENDPOINT,
                require_target=True,
                allow_fallback=False,
            ),
        )

    assert wanted.chat_calls == 0
    assert sibling.chat_calls == 0, "a required target was replaced by another endpoint"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_target_outside_the_granted_range_is_refused_not_dispatched() -> None:
    """The caller's grant bounds a required target too.

    A target the caller excluded is not dispatched and is not replaced: the
    dispatch reports that it cannot run, which is what stops a composition from
    quietly serving an endpoint the caller ruled out.
    """
    wanted = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    sibling = _adapter(_SECOND_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    router = _shared_router(wanted, sibling)

    with pytest.raises(TargetUnavailableError):
        await router.chat_completion(
            _MODEL_ID,
            _MESSAGES,
            routing_options=RoutingRequestOptions(
                preferred_endpoint_id=_LOCAL_ENDPOINT,
                require_target=True,
                allow_fallback=False,
                endpoint_scope=frozenset({_SECOND_LOCAL_ENDPOINT}),
            ),
        )

    assert wanted.chat_calls == 0
    assert sibling.chat_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_routewise_dispatch_reaches_only_the_required_endpoint() -> None:
    """RouteWise honors an exact target at its feasible-set boundary.

    Its own cost model prefers the cheaper endpoint; a required target has to
    narrow the candidate set before the solve rather than be ignored as an
    advisory preference.
    """
    wanted = _adapter(_CLOUD_ENDPOINT, provider="expensive", base_url="https://a.example/v1")
    cheaper = _adapter(_CHEAP_ENDPOINT, provider="cheap", base_url="https://b.example/v1")
    router = _routewise_router(_shared_router(wanted, cheaper))

    response = await router.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=RoutingRequestOptions(
            preferred_endpoint_id=_CLOUD_ENDPOINT,
            require_target=True,
            allow_fallback=False,
        ),
    )

    assert response["_routing"]["endpoint_id"] == _CLOUD_ENDPOINT
    assert wanted.chat_calls == 1
    assert cheaper.chat_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_binding_keeps_executing_the_adapter_it_was_taken_from() -> None:
    """A route edit cannot redirect a request that is already bound.

    The binding holds the adapter, not the endpoint id alone, so an in-flight
    request finishes on the adapter it was admitted for even after the route
    table swaps a different adapter in under the same endpoint. Capacity is
    released through the reservation the router holds, not through anything the
    binding or the leaf remembers.
    """
    original = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    router = _shared_router(original)
    binding = router.binding_for(original, _MODEL_ID)
    assert binding is not None and binding.adapter is original

    replacement = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    router.register_route(_MODEL_ID, [(replacement, 1.0)])

    leaf = LeafBackend.for_binding(binding)
    await leaf.chat_completion(_MESSAGES)

    assert original.chat_calls == 1, "the bound adapter must be the one that runs"
    assert replacement.chat_calls == 0, "a refresh redirected an in-flight request"
