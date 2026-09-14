"""Composition contracts for HybridRouter over two injected backends."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from routing.backends import LocalBackend, RouteWiseCloudBackend
from routing.hybrid import BackendSelection, HybridRouter, HybridRoutingError
from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter, RoutingObservation
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "combo-model"
_MESSAGES = [{"role": "user", "content": "hello"}]
_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-12003"
_CLOUD_ENDPOINT = f"{_MODEL_ID}:zai-api"
_SHARED_ENDPOINT = "combo-model:shared-api"


class _ComboAdapter(BaseAdapter):
    """Adapter double that records calls and replays scripted chunks."""

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
        self.chat_params: list[dict[str, Any]] = []
        self.stream_closed = False

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        self.chat_calls += 1
        self.chat_params.append(dict(params))
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
    provider: str | None = None,
    price: str = "1",
    chat_error: BaseException | None = None,
    stream_chunks: tuple[str, ...] = (),
    stream_error: BaseException | None = None,
) -> _ComboAdapter:
    config = ModelConfig(
        id=_MODEL_ID,
        name=_MODEL_ID,
        provider=provider or endpoint_id,
        base_url=f"https://{endpoint_id}.example/v1",
        endpoint_id=endpoint_id,
        pricing={"prompt": price, "completion": price},
        input_modalities=["text"],
    )
    return _ComboAdapter(
        config,
        chat_error=chat_error,
        stream_chunks=stream_chunks,
        stream_error=stream_error,
    )


def _route_table(*adapters: _ComboAdapter) -> FixedRouter:
    table = FixedRouter()
    table.register_route(_MODEL_ID, [(adapter, 1.0) for adapter in adapters])
    return table


@dataclass
class _ForceBackend(BackendSelection):
    """Test policy that always names one backend.

    Stands in for the Greedy/Nimbus policies a later task will add; the hybrid
    seam must be exercised without shipping a production policy here.
    """

    backend_name: str
    calls: list[str] = field(default_factory=list)

    def select_backend(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> str:
        self.calls.append(model_id)
        return self.backend_name


@dataclass
class _CountingBackend:
    """Minimal backend double that counts feedback and lifecycle calls.

    ``starts_before_this_call`` models a router another owner already started:
    the first start this backend attempts then reports "already running".
    """

    backend_name: str
    owns: bool = True
    observations: list[str] = field(default_factory=list)
    starts: int = 0
    stops: int = 0
    starts_before_this_call: int = 0

    @property
    def name(self) -> str:
        return self.backend_name

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        return {"choices": [], "model": model_id}

    async def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        yield "data: [DONE]\n\n"

    def record_observation(self, obs: RoutingObservation) -> None:
        self.observations.append(obs.endpoint_id)

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        return {self.backend_name: {"circuit_state": "closed"}}

    def owns_observation(self, obs: RoutingObservation) -> bool:
        return self.owns

    async def start(self) -> bool:
        self.starts += 1
        return self.starts > self.starts_before_this_call

    async def stop(self) -> bool:
        self.stops += 1
        return True


def _observation(endpoint_id: str, *, request_id: str | None = None) -> RoutingObservation:
    return RoutingObservation(
        model_id=_MODEL_ID,
        endpoint_id=endpoint_id,
        ttft_ms=10.0,
        total_latency_ms=30.0,
        token_count=4,
        success=True,
        request_id=request_id,
    )


def _hybrid(
    *,
    policy: BackendSelection,
    local: Any,
    cloud: Any,
) -> HybridRouter:
    return HybridRouter(policy=policy, local=local, cloud=cloud)


def _local_backend(adapter: _ComboAdapter) -> LocalBackend:
    return _scoped_local_backend(adapter, _LOCAL_ENDPOINT)


def _scoped_local_backend(adapter: _ComboAdapter, endpoint_id: str) -> LocalBackend:
    """Build a local backend that declares the endpoints it actually serves."""
    return LocalBackend(
        _route_table(adapter),
        endpoint_scope={endpoint_id},
        model_scope={_MODEL_ID},
    )


def _cloud_backend(*adapters: _ComboAdapter) -> RouteWiseCloudBackend:
    table = _route_table(*adapters)
    router = RouteWiseRouter(
        config=RouteWiseConfig(budget_alpha=0.0, fallback_mode="policy", random_seed=0),
    )
    return RouteWiseCloudBackend(
        router,
        table=table,
        endpoint_scope={_CLOUD_ENDPOINT},
        model_scope={_MODEL_ID},
    )


@pytest.mark.unit
def test_backend_selection_protocol_is_satisfied_by_a_test_policy() -> None:
    assert isinstance(_ForceBackend(backend_name="local"), BackendSelection)


@pytest.mark.unit
def test_hybrid_router_rejects_a_duplicate_backend_name() -> None:
    local = LocalBackend(FixedRouter(), name="same")
    cloud = LocalBackend(FixedRouter(), name="same")

    with pytest.raises(ValueError, match="duplicate backend name"):
        HybridRouter(policy=_ForceBackend("same"), local=local, cloud=cloud)


@pytest.mark.unit
def test_hybrid_router_rejects_an_empty_backend_name() -> None:
    with pytest.raises(ValueError, match="empty backend name"):
        HybridRouter(
            policy=_ForceBackend("local"),
            local=LocalBackend(FixedRouter(), name=""),
            cloud=LocalBackend(FixedRouter(), name="cloud"),
        )


@pytest.mark.unit
def test_hybrid_router_rejects_a_policy_naming_an_uninjected_backend() -> None:
    router = _hybrid(
        policy=_ForceBackend("greedy"),
        local=_local_backend(_adapter(_LOCAL_ENDPOINT)),
        cloud=_cloud_backend(_adapter(_CLOUD_ENDPOINT)),
    )

    with pytest.raises(HybridRoutingError, match="unknown backend 'greedy'"):
        router.backend_for_request(_MODEL_ID, _MESSAGES)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forced_local_policy_executes_only_the_local_backend() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    router = _hybrid(
        policy=_ForceBackend("local"),
        local=_local_backend(local),
        cloud=_cloud_backend(cloud),
    )

    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="forced-local")

    assert response["_routing"]["backend"] == "local"
    assert response["_routing"]["endpoint_id"] == _LOCAL_ENDPOINT
    assert local.chat_calls == 1
    assert cloud.chat_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forced_cloud_policy_executes_only_the_cloud_backend() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    router = _hybrid(
        policy=_ForceBackend("cloud"),
        local=_local_backend(local),
        cloud=_cloud_backend(cloud),
    )

    response = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="forced-cloud")

    assert response["_routing"]["backend"] == "cloud"
    assert response["_routing"]["endpoint_id"] == _CLOUD_ENDPOINT
    assert cloud.chat_calls == 1
    assert local.chat_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_policy_can_route_per_request_on_the_same_router() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    router = _hybrid(
        policy=_ForceBackend("local"),
        local=_local_backend(local),
        cloud=_cloud_backend(cloud),
    )

    first = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="mixed-1")
    router._policy = _ForceBackend("cloud")  # type: ignore[attr-defined]
    second = await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="mixed-2")

    assert first["_routing"]["backend"] == "local"
    assert second["_routing"]["backend"] == "cloud"
    assert local.chat_calls == 1
    assert cloud.chat_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_owned_options_never_reach_the_adapter() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    router = _hybrid(
        policy=_ForceBackend("local"),
        local=_local_backend(local),
        cloud=_cloud_backend(_adapter(_CLOUD_ENDPOINT, provider="zai")),
    )

    await router.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=RoutingRequestOptions(),
        max_tokens=32,
    )

    assert local.chat_params == [{"max_tokens": 32}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hybrid_router_does_not_retry_across_backends() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local", chat_error=RuntimeError("local down"))
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    router = _hybrid(
        policy=_ForceBackend("local"),
        local=_local_backend(local),
        cloud=_cloud_backend(cloud),
    )

    with pytest.raises(RuntimeError, match="local down"):
        await router.chat_completion(_MODEL_ID, _MESSAGES, request_id="no-cross-retry")

    assert local.chat_calls == 1
    assert cloud.chat_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_forwards_order_and_attributes_the_backend() -> None:
    first = 'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
    second = 'data: {"choices":[{"delta":{"content":"two"}}]}\n\n'
    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        stream_chunks=(first, second, "data: [DONE]\n\n"),
    )
    router = _hybrid(
        policy=_ForceBackend("local"),
        local=_local_backend(local),
        cloud=_cloud_backend(_adapter(_CLOUD_ENDPOINT, provider="zai")),
    )

    chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id="stream-order",
        )
    ]

    assert chunks[-1] == "data: [DONE]\n\n"
    assert [chunk for chunk in chunks if '"content"' in chunk] == [first, second]
    assert _routing_payloads(chunks)[0]["backend"] == "local"
    assert _routing_payloads(chunks)[0]["endpoint_id"] == _LOCAL_ENDPOINT
    assert local.stream_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_does_not_run_the_backend_before_the_first_read() -> None:
    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        stream_chunks=('data: {"choices":[{"delta":{"content":"one"}}]}\n\n',),
    )
    router = _hybrid(
        policy=_ForceBackend("local"),
        local=_local_backend(local),
        cloud=_cloud_backend(_adapter(_CLOUD_ENDPOINT, provider="zai")),
    )

    stream = router.stream_chat_completion(_MODEL_ID, _MESSAGES, request_id="stream-lazy")

    assert local.stream_calls == 0
    assert '"provider": "local"' in await anext(stream)
    assert local.stream_calls == 0
    assert '"content":"one"' in await anext(stream)
    assert local.stream_calls == 1
    await stream.aclose()  # type: ignore[attr-defined]


class _ClosingBackend:
    """Backend double exposing an inspectable, non-generator stream iterator."""

    def __init__(self, backend_name: str, chunks: tuple[str, ...]) -> None:
        self.backend_name = backend_name
        self.inner = _InnerStream(list(chunks))

    @property
    def name(self) -> str:
        return self.backend_name

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> dict[str, Any]:  # pragma: no cover - not exercised by these tests
        return {"choices": []}

    def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> _InnerStream:
        return self.inner

    def record_observation(self, obs: RoutingObservation) -> None:
        return None

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        return {}

    def owns_observation(self, obs: RoutingObservation) -> bool:
        return True


class _InnerStream:
    """Async iterator double that records whether it was closed."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self) -> _InnerStream:
        return self

    async def __anext__(self) -> str:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_closing_the_hybrid_stream_closes_the_downstream_iterator() -> None:
    backend = _ClosingBackend("cloud", ("chunk-one", "chunk-two"))
    router = _hybrid(
        policy=_ForceBackend("cloud"),
        local=_local_backend(_adapter(_LOCAL_ENDPOINT, provider="local")),
        cloud=backend,
    )
    stream = router.stream_chat_completion(_MODEL_ID, _MESSAGES, request_id="stream-close")

    assert await anext(stream) == "chunk-one"
    assert await anext(stream) == "chunk-two"
    assert backend.inner.closed is False

    await stream.aclose()  # type: ignore[attr-defined]

    assert backend.inner.closed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hybrid_streams_through_a_real_local_backend_in_order() -> None:
    first = 'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
    second = 'data: {"choices":[{"delta":{"content":"two"}}]}\n\n'
    local = _adapter(_LOCAL_ENDPOINT, provider="local", stream_chunks=(first, second))
    router = _hybrid(
        policy=_ForceBackend("local"),
        local=_local_backend(local),
        cloud=_cloud_backend(_adapter(_CLOUD_ENDPOINT, provider="zai")),
    )

    stream = router.stream_chat_completion(_MODEL_ID, _MESSAGES, request_id="stream-finalize")
    chunks = [chunk async for chunk in stream]

    assert [chunk for chunk in chunks if '"content"' in chunk] == [first, second]
    assert local.stream_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_exceptions_propagate_unchanged() -> None:
    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        stream_chunks=('data: {"choices":[{"delta":{"content":"one"}}]}\n\n',),
        stream_error=RuntimeError("stream broke"),
    )
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    router = _hybrid(
        policy=_ForceBackend("local"),
        local=_local_backend(local),
        cloud=_cloud_backend(cloud),
    )

    with pytest.raises(RuntimeError, match="stream broke"):
        async for _ in router.stream_chat_completion(
            _MODEL_ID, _MESSAGES, request_id="stream-error"
        ):
            pass

    assert cloud.chat_calls == 0


@pytest.mark.unit
def test_observation_is_recorded_by_exactly_one_owning_backend() -> None:
    local = _CountingBackend("local", owns=False)
    cloud = _CountingBackend("cloud", owns=True)
    router = _hybrid(
        policy=_ForceBackend("cloud"),
        local=local,
        cloud=cloud,
    )

    router.record_observation(_observation(_CLOUD_ENDPOINT))

    assert cloud.observations == [_CLOUD_ENDPOINT]
    assert local.observations == []


@pytest.mark.unit
def test_dispatch_record_attributes_feedback_when_both_backends_claim_the_endpoint() -> None:
    """The backend the policy chose wins over endpoint ownership.

    Two backends can both claim one endpoint id. Broadcasting the sample would
    update a learner that never served the request, so the record written at
    dispatch time is what decides.
    """
    local = _CountingBackend("local", owns=True)
    cloud = _CountingBackend("cloud", owns=True)
    router = _hybrid(policy=_ForceBackend("cloud"), local=local, cloud=cloud)
    router.select_backend_name(_MODEL_ID, _MESSAGES, request_id="shared-request")

    router.record_observation(_observation(_SHARED_ENDPOINT, request_id="shared-request"))

    assert cloud.observations == [_SHARED_ENDPOINT]
    assert local.observations == []


@pytest.mark.unit
def test_ambiguous_feedback_without_a_dispatch_record_is_dropped() -> None:
    local = _CountingBackend("local", owns=True)
    cloud = _CountingBackend("cloud", owns=True)
    router = _hybrid(policy=_ForceBackend("local"), local=local, cloud=cloud)

    router.record_observation(_observation(_SHARED_ENDPOINT))

    assert local.observations == []
    assert cloud.observations == []


@pytest.mark.unit
def test_unclaimed_observation_is_dropped_without_raising() -> None:
    local = _CountingBackend("local", owns=False)
    cloud = _CountingBackend("cloud", owns=False)
    router = _hybrid(policy=_ForceBackend("local"), local=local, cloud=cloud)

    router.record_observation(_observation(_SHARED_ENDPOINT))

    assert local.observations == []
    assert cloud.observations == []


@pytest.mark.unit
def test_cloud_backend_ignores_an_observation_outside_its_range() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    router = RouteWiseRouter(config=RouteWiseConfig(budget_alpha=0.0, random_seed=0))
    backend = RouteWiseCloudBackend(
        router,
        table=_route_table(local, cloud),
        endpoint_scope={_CLOUD_ENDPOINT},
        model_scope={_MODEL_ID},
    )

    # Recording a local observation must not raise, and must not create
    # endpoint state for a candidate this backend never routed to.
    backend.record_observation(_observation(_LOCAL_ENDPOINT))

    assert _LOCAL_ENDPOINT not in backend.get_provider_status()


@pytest.mark.unit
def test_provider_status_is_merged_across_backends() -> None:
    local = _CountingBackend("local")
    cloud = _CountingBackend("cloud")
    router = _hybrid(policy=_ForceBackend("local"), local=local, cloud=cloud)

    assert router.get_provider_status() == {
        "local": {"circuit_state": "closed"},
        "cloud": {"circuit_state": "closed"},
    }
    assert router.backend_status() == {
        "local": {"local": {"circuit_state": "closed"}},
        "cloud": {"cloud": {"circuit_state": "closed"}},
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifecycle_delegation_is_idempotent() -> None:
    local = _CountingBackend("local")
    cloud = _CountingBackend("cloud")
    router = _hybrid(policy=_ForceBackend("local"), local=local, cloud=cloud)

    assert await router.start() is True
    assert await router.start() is False
    assert await router.stop() is True
    assert await router.stop() is False

    assert (local.starts, cloud.starts) == (1, 1)
    assert (local.stops, cloud.stops) == (1, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_backend_that_reports_already_started_is_not_stopped_here() -> None:
    """A start the backend did not perform is not a stop it may perform."""
    local = _CountingBackend("local")
    already_running = _CountingBackend("cloud", starts_before_this_call=1)
    router = _hybrid(policy=_ForceBackend("local"), local=local, cloud=already_running)

    assert await router.start() is True
    assert await router.stop() is True

    assert local.starts == 1 and local.stops == 1
    assert already_running.starts == 1
    assert already_running.stops == 0


def _routing_payloads(chunks: list[str]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for chunk in chunks:
        if not chunk.startswith("data: "):
            continue
        raw = chunk.removeprefix("data: ").strip()
        if raw == "[DONE]":
            continue
        routing = json.loads(raw).get("_routing")
        if isinstance(routing, dict) and "provider" in routing:
            payloads.append(routing)
    return payloads
