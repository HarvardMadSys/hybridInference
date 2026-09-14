"""Behavioral contracts for the local and RouteWise cloud backends."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from routing.backends import LocalBackend, RouteWiseCloudBackend, RoutingBackend
from routing.protocols import RoutingRequestOptions
from routing.route_scope import scope_view_for_endpoints
from routing.routers import FixedRouter, RoutingObservation
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "hybrid-model"
_MESSAGES = [{"role": "user", "content": "hello"}]
_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-12003"
_CLOUD_ENDPOINT = f"{_MODEL_ID}:zai-api"
_CLOUD_ENDPOINT_2 = f"{_MODEL_ID}:openrouter-api"


class _BackendAdapter(BaseAdapter):
    """Adapter double that records calls and can fail on demand."""

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
        for chunk in self.stream_chunks:
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error


def _adapter(
    endpoint_id: str,
    *,
    provider: str | None = None,
    price: str = "1",
    chat_error: BaseException | None = None,
    stream_chunks: tuple[str, ...] = (),
) -> _BackendAdapter:
    config = ModelConfig(
        id=_MODEL_ID,
        name=_MODEL_ID,
        provider=provider or endpoint_id,
        base_url=f"https://{endpoint_id}.example/v1",
        endpoint_id=endpoint_id,
        pricing={"prompt": price, "completion": price},
        input_modalities=["text"],
    )
    return _BackendAdapter(config, chat_error=chat_error, stream_chunks=stream_chunks)


def _route_table(*adapters: _BackendAdapter) -> FixedRouter:
    table = FixedRouter()
    table.register_route(_MODEL_ID, [(adapter, 1.0) for adapter in adapters])
    return table


def _cloud_backend(
    table: FixedRouter,
    *,
    endpoint_scope: frozenset[str] = frozenset({_CLOUD_ENDPOINT, _CLOUD_ENDPOINT_2}),
) -> RouteWiseCloudBackend:
    router = RouteWiseRouter(
        config=RouteWiseConfig(
            budget_alpha=0.0,
            fallback_mode="policy",
            random_seed=0,
        ),
    )
    return RouteWiseCloudBackend(
        router,
        table=table,
        endpoint_scope=endpoint_scope,
        model_scope={_MODEL_ID},
    )


def _observation(endpoint_id: str, *, success: bool = True) -> RoutingObservation:
    return RoutingObservation(
        model_id=_MODEL_ID,
        endpoint_id=endpoint_id,
        ttft_ms=12.0,
        total_latency_ms=40.0,
        token_count=8,
        success=success,
    )


@pytest.mark.unit
def test_local_backend_satisfies_the_backend_protocol() -> None:
    backend = LocalBackend(FixedRouter())

    assert isinstance(backend, RoutingBackend)
    assert backend.name == "local"
    assert backend.router is not None


@pytest.mark.unit
def test_local_backend_rejects_a_router_missing_the_request_contract() -> None:
    with pytest.raises(TypeError, match="chat_completion"):
        LocalBackend(object())  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_backend_delegates_without_rewriting_request_arguments() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    backend = LocalBackend(_route_table(local))
    options = RoutingRequestOptions()

    response = await backend.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=options,
        max_tokens=64,
        request_id="local-1",
    )

    assert response["_routing"]["provider"] == "local"
    assert local.chat_params == [{"max_tokens": 64, "request_id": "local-1"}]
    assert all("routing_options" not in params for params in local.chat_params)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_backend_forwards_the_router_iterator_without_buffering() -> None:
    local = _adapter(
        _LOCAL_ENDPOINT,
        provider="local",
        stream_chunks=('data: {"choices":[{"delta":{"content":"a"}}]}\n\n',),
    )
    backend = LocalBackend(_route_table(local))

    stream = backend.stream_chat_completion(_MODEL_ID, _MESSAGES)

    # Nothing runs until the consumer asks for the first chunk: the backend
    # hands over the router's own iterator instead of draining it. The router's
    # first chunk is metadata, so the adapter is still untouched after it.
    assert local.stream_calls == 0
    assert '"provider": "local"' in await anext(stream)
    assert local.stream_calls == 0
    assert '"content":"a"' in await anext(stream)
    assert local.stream_calls == 1
    await stream.aclose()  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_backend_preserves_upstream_error_semantics() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local", chat_error=RuntimeError("local down"))
    backend = LocalBackend(_route_table(local))

    with pytest.raises(RuntimeError, match="local down") as exc_info:
        await backend.chat_completion(_MODEL_ID, _MESSAGES, request_id="local-error")

    assert getattr(exc_info.value, "_routing", {})["endpoint_id"] == _LOCAL_ENDPOINT


@pytest.mark.unit
def test_local_backend_owns_every_observation_routed_to_it() -> None:
    backend = LocalBackend(_route_table(_adapter(_LOCAL_ENDPOINT, provider="local")))

    assert backend.owns_observation(_observation(_LOCAL_ENDPOINT))


@pytest.mark.unit
def test_cloud_backend_requires_an_explicit_endpoint_scope() -> None:
    table = _route_table(_adapter(_CLOUD_ENDPOINT))

    with pytest.raises(ValueError, match="non-empty endpoint_scope"):
        RouteWiseCloudBackend(
            RouteWiseRouter(config=RouteWiseConfig()), table=table, endpoint_scope=set()
        )


@pytest.mark.unit
def test_cloud_backend_view_exposes_only_the_declared_cloud_candidates() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    backend = _cloud_backend(_route_table(local, cloud))

    assert isinstance(backend, RoutingBackend)
    assert backend.name == "cloud"
    assert backend.endpoint_scope == frozenset({_CLOUD_ENDPOINT, _CLOUD_ENDPOINT_2})
    assert backend.allowed_endpoints() == frozenset({_CLOUD_ENDPOINT})
    assert [route.canonical_model_id for route in backend.view.iter_effective_routes()] == [
        _MODEL_ID
    ]
    assert [adapter for adapter, _ in backend.view.iter_effective_routes()[0].adapters] == [cloud]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cloud_backend_never_dispatches_a_local_candidate() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai", stream_chunks=("data: [DONE]\n\n",))
    backend = _cloud_backend(_route_table(local, cloud))

    response = await backend.chat_completion(_MODEL_ID, _MESSAGES, request_id="cloud-only")

    assert response["_routing"]["endpoint_id"] == _CLOUD_ENDPOINT
    assert cloud.chat_calls == 1
    assert local.chat_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cloud_backend_fallback_stays_inside_the_cloud_range() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    primary = _adapter(_CLOUD_ENDPOINT, provider="zai", chat_error=ConnectionError("zai down"))
    backup = _adapter(
        _CLOUD_ENDPOINT_2,
        provider="openrouter",
        chat_error=ConnectionError("openrouter down"),
    )
    backend = _cloud_backend(_route_table(local, primary, backup))

    with pytest.raises(ConnectionError) as exc_info:
        await backend.chat_completion(_MODEL_ID, _MESSAGES, request_id="cloud-fallback")

    routing = getattr(exc_info.value, "_routing", {})
    attempted = {attempt["endpoint_id"] for attempt in routing["failed_attempts"]}

    assert attempted
    assert attempted <= {_CLOUD_ENDPOINT, _CLOUD_ENDPOINT_2}
    assert local.chat_calls == 0
    assert primary.chat_calls + backup.chat_calls == len(attempted)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cloud_backend_excludes_local_endpoints_from_background_probes() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    backend = _cloud_backend(_route_table(local, cloud))

    results = await backend.router.run_probe_once(idle_only=False)

    assert [result.endpoint_id for result in results] == [_CLOUD_ENDPOINT]
    assert local.chat_calls == 0


@pytest.mark.unit
def test_cloud_backend_owns_only_observations_inside_its_range() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    backend = _cloud_backend(_route_table(local, cloud))

    assert backend.owns_observation(_observation(_CLOUD_ENDPOINT))
    assert not backend.owns_observation(_observation(_LOCAL_ENDPOINT))
    assert not backend.owns_model("another-model")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cloud_backend_reports_the_wrapped_router_health_state() -> None:
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    table = _route_table(cloud)
    backend = _cloud_backend(table, endpoint_scope=frozenset({_CLOUD_ENDPOINT}))

    await backend.chat_completion(_MODEL_ID, _MESSAGES, request_id="cloud-health")

    assert backend.get_provider_status() == backend.router.get_provider_status()
    assert backend.get_provider_status()[_CLOUD_ENDPOINT]["circuit_state"] == "closed"
    assert backend.canonical_id(_MODEL_ID) == _MODEL_ID


@pytest.mark.unit
def test_cloud_backend_refresh_rebuilds_the_projection() -> None:
    first = _adapter(_CLOUD_ENDPOINT, provider="zai")
    table = _route_table(first)
    backend = _cloud_backend(table, endpoint_scope=frozenset({_CLOUD_ENDPOINT, _CLOUD_ENDPOINT_2}))
    original_view = backend.view

    second = _adapter(_CLOUD_ENDPOINT_2, provider="openrouter")
    table.register_route(_MODEL_ID, [(first, 1.0), (second, 1.0)])
    backend.refresh_route_table()

    assert backend.view is not original_view
    assert backend.allowed_endpoints() == frozenset({_CLOUD_ENDPOINT, _CLOUD_ENDPOINT_2})


@pytest.mark.unit
def test_scope_view_for_endpoints_is_the_documented_construction_helper() -> None:
    local = _adapter(_LOCAL_ENDPOINT, provider="local")
    cloud = _adapter(_CLOUD_ENDPOINT, provider="zai")
    table = _route_table(local, cloud)

    view = scope_view_for_endpoints(table, {_CLOUD_ENDPOINT})

    assert [adapter for adapter, _ in view.iter_effective_routes()[0].adapters] == [cloud]
