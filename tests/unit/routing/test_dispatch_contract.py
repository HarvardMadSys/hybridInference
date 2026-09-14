"""The dispatch boundary: what a router may ask a backend to do, and what it may not.

Two instructions exist and they are not interchangeable. ``ExecuteEndpoint``
binds one endpoint and ends the recursion; ``DelegatePool`` grants selection
inside a named pool. A leaf backend takes only the first, a pool backend only the
second, and a mismatch is refused before any upstream I/O -- a composition error,
never an upstream fault.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from routing.backends import FixedCloudBackend, LeafBackend, LocalBackend, TreeBackend
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
