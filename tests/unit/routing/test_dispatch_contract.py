"""The dispatch boundary: what a router may ask a backend to do, and what it may not.

Two instructions exist and they are not interchangeable. ``ExecuteEndpoint``
binds one endpoint and ends the recursion; ``DelegatePool`` grants selection
inside a named pool. A leaf backend takes only the first, a pool backend only the
second, and a mismatch is refused before any upstream I/O -- a composition error,
never an upstream fault.
"""

from __future__ import annotations

import json
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
    accepts_delegation,
    backend_pool_id,
    bound_endpoint,
    check_dispatch,
    dispatch_for_attempt,
)
from routing.endpoint_health import EndpointHealthRegistry
from routing.endpoints import endpoint_id_for_adapter
from routing.hybrid import HybridRouter
from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "compose-model"
_MESSAGES = [{"role": "user", "content": "hello"}]
_LOCAL_URL = "http://localhost:11434/v1"
_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-11434"
_SECOND_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-11500"
_CLOUD_ENDPOINT = f"{_MODEL_ID}:zai-api"


class _RecordingAdapter(BaseAdapter):
    """Adapter double that counts dispatches and can fail on demand."""

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
        if self.stream_error is not None:
            raise self.stream_error
        yield 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'


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


class _DomainOnlyPolicy:
    """Policy that classifies domains and names no candidate at all."""

    def select_backend(self, *args: Any, **kwargs: Any) -> RoutingDecision:
        return RoutingDecision(backend="local")

    def fallback_backends(self, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        return ("cloud",)


# ---------------------------------------------------------------------------
# The compatibility mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_exact_attempt_maps_to_an_endpoint_binding() -> None:
    instruction = dispatch_for_attempt(
        pool_id="cloud-model-b",
        model_id=_MODEL_ID,
        endpoint_id=_CLOUD_ENDPOINT,
        exact=True,
    )

    assert isinstance(instruction, ExecuteEndpoint)
    assert instruction.binding == EndpointBinding(
        endpoint_id=_CLOUD_ENDPOINT,
        model_id=_MODEL_ID,
        pool_id="cloud-model-b",
    )


@pytest.mark.unit
def test_an_exact_attempt_without_an_endpoint_is_refused() -> None:
    """A hard target that names nothing cannot be expressed as a delegation."""
    with pytest.raises(DispatchMismatchError, match="requires an endpoint_id"):
        dispatch_for_attempt(
            pool_id="cloud",
            model_id=_MODEL_ID,
            endpoint_id=None,
            exact=True,
        )


@pytest.mark.unit
def test_a_delegating_attempt_maps_to_a_pool_delegation() -> None:
    instruction = dispatch_for_attempt(
        pool_id="cloud",
        model_id=_MODEL_ID,
        endpoint_id=None,
        exact=False,
    )

    assert instruction == DelegatePool(pool_id="cloud")


@pytest.mark.unit
def test_a_preference_stays_a_delegation() -> None:
    """Mapping a preference onto a hard target is the mistake this guards.

    An endpoint the caller merely prefers may still be replaced by the pool's own
    selection. Only ``require_target`` -- an exact attempt -- keeps it.
    """
    instruction = dispatch_for_attempt(
        pool_id="cloud",
        model_id=_MODEL_ID,
        endpoint_id=_CLOUD_ENDPOINT,
        exact=False,
    )

    assert isinstance(instruction, DelegatePool)


@pytest.mark.unit
def test_role_defaults_keep_the_existing_wrappers_delegating() -> None:
    """A backend that declares no role keeps the compatibility behavior."""

    class _Undeclared:
        name = "undeclared"

    assert accepts_delegation(_Undeclared()) is True
    assert bound_endpoint(_Undeclared()) is None
    assert backend_pool_id(_Undeclared()) == "undeclared"


# ---------------------------------------------------------------------------
# LeafBackend
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_leaf_declares_that_it_takes_no_delegation() -> None:
    leaf = LeafBackend(
        _shared_router(_adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)),
        endpoint_id=_LOCAL_ENDPOINT,
        model_id=_MODEL_ID,
    )

    assert accepts_delegation(leaf) is False
    assert bound_endpoint(leaf) == _LOCAL_ENDPOINT
    assert leaf.dispatch_scope(_MODEL_ID) == frozenset({_LOCAL_ENDPOINT})


@pytest.mark.unit
def test_a_leaf_refuses_a_pool_delegation() -> None:
    leaf = LeafBackend(
        _shared_router(_adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)),
        endpoint_id=_LOCAL_ENDPOINT,
        model_id=_MODEL_ID,
    )

    with pytest.raises(DispatchMismatchError, match="has no pool to delegate to"):
        check_dispatch(leaf, DelegatePool(pool_id="local"), _MODEL_ID)


@pytest.mark.unit
def test_a_leaf_refuses_a_binding_for_another_endpoint() -> None:
    leaf = LeafBackend(
        _shared_router(_adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)),
        endpoint_id=_LOCAL_ENDPOINT,
        model_id=_MODEL_ID,
    )

    with pytest.raises(DispatchMismatchError, match="was asked to execute"):
        check_dispatch(
            leaf,
            ExecuteEndpoint(
                EndpointBinding(endpoint_id=_SECOND_LOCAL_ENDPOINT, model_id=_MODEL_ID)
            ),
            _MODEL_ID,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_leaf_executes_only_its_bound_endpoint() -> None:
    """A failing leaf does not fall back: the recursion ends at one endpoint."""
    bound = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("bound endpoint down"),
    )
    sibling = _adapter(_SECOND_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    leaf = LeafBackend(
        _shared_router(bound, sibling),
        endpoint_id=_LOCAL_ENDPOINT,
        model_id=_MODEL_ID,
    )

    with pytest.raises(ConnectionError):
        await leaf.chat_completion(_MODEL_ID, _MESSAGES)

    assert bound.chat_calls == 1
    assert sibling.chat_calls == 0, "a leaf walked past its binding"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_leaf_sends_a_binding_instruction_to_its_router() -> None:
    """The dispatch is binding, not advisory: exact target, no fallback."""

    class _OptionsRecorder:
        name = "recorder"
        # Declares the capability a leaf requires; this double exists to assert
        # what a capable router is sent.
        supports_exact_dispatch = True

        def __init__(self) -> None:
            self.seen: list[RoutingRequestOptions | None] = []

        async def chat_completion(self, model_id: str, messages: Any, **kwargs: Any) -> Any:
            self.seen.append(kwargs.get("routing_options"))
            return {}

        def stream_chat_completion(self, model_id: str, messages: Any, **kwargs: Any) -> Any:
            self.seen.append(kwargs.get("routing_options"))
            return None

        def record_observation(self, obs: Any) -> None:
            pass

        def get_provider_status(self) -> dict[str, Any]:
            return {}

    recorder = _OptionsRecorder()
    leaf = LeafBackend(recorder, endpoint_id=_LOCAL_ENDPOINT, model_id=_MODEL_ID)

    await leaf.chat_completion(_MODEL_ID, _MESSAGES)

    sent = recorder.seen[0]
    assert sent is not None
    assert sent.preferred_endpoint_id == _LOCAL_ENDPOINT
    assert sent.require_target is True, "the router could still swap the endpoint"
    assert sent.allow_fallback is False, "the router could still walk the route"
    assert sent.endpoint_scope == frozenset({_LOCAL_ENDPOINT})


# ---------------------------------------------------------------------------
# TreeBackend, and the wrappers that are built on it
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_existing_wrappers_are_pools() -> None:
    """Local and cloud wrappers delegate, so they are pool backends."""
    shared = _shared_router(
        _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL),
        _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1"),
    )
    local = LocalBackend(shared, endpoint_scope={_LOCAL_ENDPOINT}, model_scope={_MODEL_ID})
    cloud = FixedCloudBackend(shared, endpoint_scope={_CLOUD_ENDPOINT}, model_scope={_MODEL_ID})

    assert isinstance(local, TreeBackend)
    assert isinstance(cloud, TreeBackend)
    assert accepts_delegation(local) is True
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
def test_a_pool_accepts_its_range_and_refuses_a_binding_outside_it() -> None:
    local = LocalBackend(
        _shared_router(
            _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL),
            _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1"),
        ),
        endpoint_scope={_LOCAL_ENDPOINT},
        model_scope={_MODEL_ID},
    )

    check_dispatch(
        local,
        ExecuteEndpoint(EndpointBinding(endpoint_id=_LOCAL_ENDPOINT, model_id=_MODEL_ID)),
        _MODEL_ID,
    )
    with pytest.raises(DispatchMismatchError, match="may dispatch inside"):
        check_dispatch(
            local,
            ExecuteEndpoint(EndpointBinding(endpoint_id=_CLOUD_ENDPOINT, model_id=_MODEL_ID)),
            _MODEL_ID,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_pool_delegation_lets_its_router_choose_inside_the_range() -> None:
    """A delegation grants selection -- within the pool, and no further."""
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


@pytest.mark.unit
def test_a_pool_refuses_a_range_that_excludes_everything_it_serves() -> None:
    """A delegation that grants nothing this pool can serve is a composition error."""
    local = LocalBackend(
        _shared_router(_adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)),
        endpoint_scope={_LOCAL_ENDPOINT},
        model_scope={_MODEL_ID},
    )
    narrowed_away = RoutingRequestOptions(endpoint_scope=frozenset({_CLOUD_ENDPOINT}))

    with pytest.raises(DispatchMismatchError, match="was restricted to"):
        local._scoped_options(narrowed_away, _MODEL_ID)


@pytest.mark.unit
def test_a_caller_scope_can_narrow_a_pool_but_never_widen_it() -> None:
    """The effective range is the intersection, so a child cannot exceed its grant."""
    first = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    second = _adapter(_SECOND_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    local = LocalBackend(
        _shared_router(first, second),
        endpoint_scope={_LOCAL_ENDPOINT, _SECOND_LOCAL_ENDPOINT},
        model_scope={_MODEL_ID},
    )

    narrowed = local._scoped_options(
        RoutingRequestOptions(endpoint_scope=frozenset({_SECOND_LOCAL_ENDPOINT})),
        _MODEL_ID,
    )
    assert narrowed is not None
    assert narrowed.endpoint_scope == frozenset({_SECOND_LOCAL_ENDPOINT})

    # A wider grant stays at the pool's own range; the pool never reaches past it.
    widened = local._scoped_options(
        RoutingRequestOptions(endpoint_scope=frozenset({_CLOUD_ENDPOINT}) | {_LOCAL_ENDPOINT}),
        _MODEL_ID,
    )
    assert widened is not None
    assert widened.endpoint_scope == frozenset({_LOCAL_ENDPOINT})


# ---------------------------------------------------------------------------
# A leaf's binding, against routers and ranges it does not control
# ---------------------------------------------------------------------------


class _IncapableRouter:
    """A router that declares no exact-dispatch capability."""

    name = "incapable"

    async def chat_completion(self, model_id: str, messages: Any, **kwargs: Any) -> Any:
        return {}

    def stream_chat_completion(self, model_id: str, messages: Any, **kwargs: Any) -> Any:
        return None

    def record_observation(self, obs: Any) -> None:
        pass

    def get_provider_status(self) -> dict[str, Any]:
        return {}


@pytest.mark.unit
def test_a_leaf_refuses_a_router_that_cannot_dispatch_exactly() -> None:
    """The controls a leaf sends are a request, and it requires them to be honored.

    Handing them to a router that ignores them would dispatch to an endpoint
    nobody asked for, so the leaf refuses at construction instead of accepting a
    binding it cannot keep.
    """
    with pytest.raises(DispatchMismatchError, match="does not declare supports_exact_dispatch"):
        LeafBackend(_IncapableRouter(), endpoint_id=_LOCAL_ENDPOINT, model_id=_MODEL_ID)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_leaf_never_widens_the_range_the_caller_granted() -> None:
    """A caller that excluded this endpoint is not overruled by the binding."""
    bound = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    leaf = LeafBackend(_shared_router(bound), endpoint_id=_LOCAL_ENDPOINT, model_id=_MODEL_ID)

    with pytest.raises(DispatchMismatchError, match="outside the range the caller granted"):
        await leaf.chat_completion(
            _MODEL_ID,
            _MESSAGES,
            routing_options=RoutingRequestOptions(endpoint_scope=frozenset({_CLOUD_ENDPOINT})),
        )

    assert bound.chat_calls == 0, "the excluded endpoint was dispatched anyway"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_leaf_accepts_a_label_range_that_covers_its_binding() -> None:
    """A provider label is the same grant as the endpoints it resolves to."""
    bound = _adapter(_LOCAL_ENDPOINT, provider="owned", base_url=_LOCAL_URL)
    leaf = LeafBackend(_shared_router(bound), endpoint_id=_LOCAL_ENDPOINT, model_id=_MODEL_ID)

    await leaf.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=RoutingRequestOptions(endpoint_scope=frozenset({"owned"})),
    )

    assert bound.chat_calls == 1


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

    check_dispatch(
        pool,
        ExecuteEndpoint(EndpointBinding(endpoint_id=_CLOUD_ENDPOINT, model_id=_MODEL_ID)),
        _MODEL_ID,
    )


def _routewise_router(table: FixedRouter) -> Any:
    """Build a RouteWise router over ``table``."""
    from routing.routewise.config import RouteWiseConfig
    from routing.routewise.router import RouteWiseRouter

    return RouteWiseRouter(
        route_table=table,
        config=RouteWiseConfig(budget_alpha=0.0, random_seed=0, routewise_probe_enabled=False),
    )


def _routewise_pool(*adapters: _RecordingAdapter) -> RouteWiseCloudBackend:
    """Build a scoped RouteWise wrapper over ``adapters``."""
    table = _shared_router(*adapters)
    return RouteWiseCloudBackend(
        _routewise_router(table),
        table=table,
        endpoint_scope={endpoint_id_for_adapter(adapter) for adapter in adapters},
        model_scope={_MODEL_ID},
    )


async def _drain(router: Any, options: Any, *, collect: bool = False) -> Any:
    """Consume a stream, returning the chunks when asked for them."""
    chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            _MODEL_ID, _MESSAGES, routing_options=options, request_id="drain"
        )
    ]
    return chunks if collect else None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_narrowed_caller_range_reaches_a_routewise_pool(streaming: bool) -> None:
    """A pool's own router consumes the narrowed range, not just the wrapper.

    Computing an intersection the sub-router then ignores would let a request be
    served by an endpoint the caller excluded. The decision is made over the
    candidate set, so the range has to reach it -- passing it along in the
    request options is not enough on its own.
    """
    expensive = _adapter(_CLOUD_ENDPOINT, provider="expensive", base_url="https://a.example/v1")
    cheap = _adapter(f"{_MODEL_ID}:cloud-b", provider="cheap", base_url="https://b.example/v1")
    pool = _routewise_pool(expensive, cheap)

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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_composition_error_is_not_recorded_as_a_failed_attempt() -> None:
    """An empty pool is a composition mistake, not an upstream failure to retry.

    Recording it would report a provider fault for an endpoint nothing was ever
    sent to, and falling through would let the request be served by a backend the
    composition never chose for it.
    """
    local_adapter = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    cloud_adapter = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local_adapter, cloud_adapter)
    router = HybridRouter(
        policy=_DomainOnlyPolicy(),
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
        policy=_DomainOnlyPolicy(),
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


def _routing_metadata(result: Any) -> list[dict[str, Any]]:
    """Collect every ``_routing`` payload a response or stream carried."""
    if isinstance(result, dict):
        return [result.get("_routing", {})]
    found: list[dict[str, Any]] = []
    for chunk in result:
        if isinstance(chunk, str) and chunk.startswith("data: "):
            raw = chunk[6:].strip()
            if raw == "[DONE]":
                continue
            found.append(json.loads(raw).get("_routing", {}))
    return found


class _DelegateToCloudPolicy:
    """Policy that hands the request to the cloud pool and nothing else."""

    def select_backend(self, *args: Any, **kwargs: Any) -> RoutingDecision:
        return RoutingDecision(backend="cloud")

    def fallback_backends(self, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        return ()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_the_caller_range_survives_the_composition_to_a_pool(streaming: bool) -> None:
    """The hybrid layer forwards a caller's grant, it does not replace it.

    Replacing it with the pool's own range hands the pool back a wider grant
    than the caller made, and the pool's intersection check cannot recover a
    restriction it never received -- the request is then served by an endpoint
    the caller excluded.
    """
    local = _adapter(_LOCAL_ENDPOINT, provider="owned", base_url=_LOCAL_URL)
    allowed = _adapter(_CLOUD_ENDPOINT, provider="expensive", base_url="https://a.example/v1")
    cheaper = _adapter(f"{_MODEL_ID}:cloud-b", provider="cheap", base_url="https://b.example/v1")
    shared = _shared_router(local, allowed, cheaper)
    router = HybridRouter(
        policy=_DelegateToCloudPolicy(),
        local=LocalBackend(shared, endpoint_scope={_LOCAL_ENDPOINT}, model_scope={_MODEL_ID}),
        cloud=RouteWiseCloudBackend(
            _routewise_router(shared),
            table=shared,
            endpoint_scope={_CLOUD_ENDPOINT, f"{_MODEL_ID}:cloud-b"},
            model_scope={_MODEL_ID},
        ),
    )

    options = RoutingRequestOptions(endpoint_scope=frozenset({_CLOUD_ENDPOINT}))
    if streaming:
        await _drain(router, options)
    else:
        await router.chat_completion(_MODEL_ID, _MESSAGES, routing_options=options)

    assert allowed.chat_calls + allowed.stream_calls == 1
    assert cheaper.chat_calls + cheaper.stream_calls == 0, (
        "the composition widened the range the caller granted"
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_busy_exact_target_is_not_a_provider_failure(streaming: bool) -> None:
    """A configured endpoint whose capacity is spent has not failed.

    Nothing was sent to it, so recording an attempt would invent an upstream
    fault and poison the feedback and the failure history. The plan moves on
    instead, and the capacity another request holds stays held.
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
        local=LeafBackend(
            local_router, endpoint_id=_LOCAL_ENDPOINT, model_id=_MODEL_ID, name="local"
        ),
        cloud=FixedCloudBackend(table, endpoint_scope={_CLOUD_ENDPOINT}, model_scope={_MODEL_ID}),
    )
    try:
        result = (
            await _drain(router, None, collect=True)
            if streaming
            else await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="busy-exact")
        )
        assert local.chat_calls == 0 and local.stream_calls == 0
        assert cloud.chat_calls + cloud.stream_calls == 1
        assert pool.active == 1, "another request's occupied slot was released"
        failures = [
            attempt
            for metadata in _routing_metadata(result)
            for attempt in metadata.get("failed_attempts", [])
        ]
        assert not failures, f"nothing was sent, but attempts were recorded: {failures}"
    finally:
        pool.release()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_busy_pool_delegation_is_not_a_provider_failure(streaming: bool) -> None:
    """A pool with no capacity left has not failed either.

    Whether the attempt was an exact binding or a delegation does not change
    whether an upstream was contacted, and only that decides whether there is a
    provider fault to record. The plan keeps going and the capacity another
    request holds stays held.
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
        result = (
            await _drain(router, None, collect=True)
            if streaming
            else await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="busy-pool")
        )
        assert local.chat_calls == 0 and local.stream_calls == 0
        assert cloud.chat_calls + cloud.stream_calls == 1
        assert pool.active == 1, "another request's occupied slot was released"
        failures = [
            attempt
            for metadata in _routing_metadata(result)
            for attempt in metadata.get("failed_attempts", [])
        ]
        assert not failures, f"nothing was sent, but attempts were recorded: {failures}"
    finally:
        pool.release()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_plan_that_was_never_dispatched_still_reports_why() -> None:
    """Dropping the failed sample must not drop the reason.

    With every candidate refused, the caller is told that nothing was admissible
    -- the refusal itself -- rather than a generic "every backend failed".
    """
    import time as _time

    from routing.endpoint_health import _CircuitState
    from routing.routers import AllCircuitsOpenError

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


# ---------------------------------------------------------------------------
# The boundary inside a composition
# ---------------------------------------------------------------------------


def _leaf_and_pool_router(
    policy: Any,
    local_leaf: LeafBackend,
    cloud_pool: Any,
) -> HybridRouter:
    return HybridRouter(policy=policy, local=local_leaf, cloud=cloud_pool)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_mismatched_instruction_is_refused_before_any_upstream_io() -> None:
    """A domain-only policy hands the leaf a delegation, and the leaf refuses.

    Nothing has been sent when this is raised, so it must surface as a
    composition error rather than being recorded as an attempted upstream.
    """
    local_adapter = _adapter(_LOCAL_ENDPOINT, provider="local", base_url=_LOCAL_URL)
    cloud_adapter = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local_adapter, cloud_adapter)
    router = _leaf_and_pool_router(
        _DomainOnlyPolicy(),
        LeafBackend(shared, endpoint_id=_LOCAL_ENDPOINT, model_id=_MODEL_ID, name="local"),
        FixedCloudBackend(shared, endpoint_scope={_CLOUD_ENDPOINT}, model_scope={_MODEL_ID}),
    )

    with pytest.raises(DispatchMismatchError):
        await router.chat_completion(_MODEL_ID, _MESSAGES)

    assert local_adapter.chat_calls == 0
    assert cloud_adapter.chat_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_leaf_primary_runs_exactly_and_the_pool_takes_the_fallback() -> None:
    """The two roles cooperate: execute locally, delegate to the cloud pool.

    This is the shape the design describes -- a leaf for the endpoint the caller
    has already chosen, a pool for the side that selects for itself -- and it
    only works because each attempt states which of the two it is.
    """
    local_adapter = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        base_url=_LOCAL_URL,
        chat_error=ConnectionError("local down"),
    )
    cloud_adapter = _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1")
    shared = _shared_router(local_adapter, cloud_adapter)
    router = _leaf_and_pool_router(
        _PlannedPolicy(
            primary="local",
            primary_endpoint=_LOCAL_ENDPOINT,
            fallback_endpoint=_CLOUD_ENDPOINT,
        ),
        LeafBackend(shared, endpoint_id=_LOCAL_ENDPOINT, model_id=_MODEL_ID, name="local"),
        FixedCloudBackend(shared, endpoint_scope={_CLOUD_ENDPOINT}, model_scope={_MODEL_ID}),
    )

    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="two-role")

    assert response["_routing"]["endpoint_id"] == _CLOUD_ENDPOINT
    assert response["_routing"]["backend"] == "cloud"
    assert local_adapter.chat_calls == 1
    assert cloud_adapter.chat_calls == 1
