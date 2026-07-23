"""Shared behavioral contracts for every serving router implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from routing.protocols import RouterProtocol, RouteTableRefreshable, RoutingRequestOptions
from routing.route_table import RouteTableView
from routing.routers import AllCircuitsOpenError, FixedRouter
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

_MODEL_ID = "contract-model"
_MESSAGES = [{"role": "user", "content": "hello"}]


class _ContractAdapter(BaseAdapter):
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
        self.stream_params: list[dict[str, Any]] = []

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
        self.stream_params.append(dict(params))
        for chunk in self.stream_chunks:
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error


class _StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _adapter(
    provider: str,
    *,
    price: str = "1",
    input_modalities: list[str] | None = None,
    chat_error: BaseException | None = None,
    stream_chunks: tuple[str, ...] = (),
    stream_error: BaseException | None = None,
) -> _ContractAdapter:
    config = ModelConfig(
        id=_MODEL_ID,
        name=_MODEL_ID,
        provider=provider,
        base_url=f"https://{provider}.example/v1",
        endpoint_id=f"{_MODEL_ID}:{provider}",
        pricing={"prompt": price, "completion": price},
        input_modalities=input_modalities or ["text"],
    )
    return _ContractAdapter(
        config,
        chat_error=chat_error,
        stream_chunks=stream_chunks,
        stream_error=stream_error,
    )


@dataclass(frozen=True)
class _RouterFactory:
    name: str
    build: Callable[[list[_ContractAdapter]], RouterProtocol]


@pytest.fixture(params=("fixed", "routewise"))
def router_factory(request: pytest.FixtureRequest) -> _RouterFactory:
    def build_fixed(adapters: list[_ContractAdapter]) -> FixedRouter:
        router = FixedRouter()
        # Keep later adapters eligible for fallback while making the first
        # selection deterministic for the contract scenarios.
        weights = [1e100, *(1.0 for _ in adapters[1:])]
        router.register_route(_MODEL_ID, list(zip(adapters, weights, strict=True)))
        return router

    def build_routewise(adapters: list[_ContractAdapter]) -> RouteWiseRouter:
        route_table = FixedRouter()
        route_table.register_route(
            _MODEL_ID,
            [(adapter, 1.0) for adapter in adapters],
        )
        return RouteWiseRouter(
            route_table=route_table,
            config=RouteWiseConfig(
                budget_alpha=0.0,
                fallback_mode="policy",
                random_seed=0,
            ),
        )

    if request.param == "fixed":
        return _RouterFactory(name="fixed", build=build_fixed)
    return _RouterFactory(name="routewise", build=build_routewise)


@pytest.mark.unit
def test_serving_routers_satisfy_structural_protocol(router_factory: _RouterFactory) -> None:
    router = router_factory.build([_adapter("primary")])

    assert isinstance(router, RouterProtocol)


@pytest.mark.unit
def test_routewise_router_exposes_public_route_table_refresh_capability() -> None:
    routewise = RouteWiseRouter(config=RouteWiseConfig())

    assert isinstance(routewise, RouteTableRefreshable)
    assert not isinstance(FixedRouter(), RouteTableRefreshable)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_routing_options_are_consumed_before_adapter_dispatch(
    router_factory: _RouterFactory,
) -> None:
    primary = _adapter(
        "primary",
        stream_chunks=(
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            "data: [DONE]\n\n",
        ),
    )
    router = router_factory.build([primary])
    options = RoutingRequestOptions()

    await router.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=options,
    )
    async for _ in router.stream_chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=options,
    ):
        pass

    assert all("routing_options" not in params for params in primary.chat_params)
    assert all("routing_options" not in params for params in primary.stream_params)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_required_modalities_filter_chat_and_stream_routes(
    router_factory: _RouterFactory,
) -> None:
    text_only = _adapter(
        "text-only",
        price="0.001",
        stream_chunks=('data: {"choices":[{"delta":{"content":"wrong"}}]}\n\n',),
    )
    vision = _adapter(
        "vision",
        price="100",
        input_modalities=["text", "image"],
        stream_chunks=(
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            "data: [DONE]\n\n",
        ),
    )
    router = router_factory.build([text_only, vision])
    options = RoutingRequestOptions(required_modalities=frozenset({"image"}))

    response = await router.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        routing_options=options,
    )
    chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            _MODEL_ID,
            _MESSAGES,
            routing_options=options,
        )
    ]

    assert response["_routing"]["provider"] == "vision"
    assert any(routing["provider"] == "vision" for routing in _routing_payloads(chunks))
    assert text_only.chat_calls == 0
    assert text_only.stream_calls == 0
    assert vision.chat_calls == 1
    assert vision.stream_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_required_modalities_raise_when_no_route_supports_them(
    router_factory: _RouterFactory,
) -> None:
    router = router_factory.build([_adapter("text-only")])

    with pytest.raises(AllCircuitsOpenError, match="accepts input modalities"):
        await router.chat_completion(
            _MODEL_ID,
            _MESSAGES,
            routing_options=RoutingRequestOptions(
                required_modalities=frozenset({"image"}),
            ),
        )


@pytest.mark.unit
def test_fixed_router_satisfies_route_table_view_and_returns_canonical_snapshot() -> None:
    primary = _adapter("primary")
    route_table = FixedRouter()
    route_table.register_route(_MODEL_ID, [(primary, 1.0)], aliases=["contract-alias"])

    snapshot = route_table.iter_effective_routes()

    assert isinstance(route_table, RouteTableView)
    assert len(snapshot) == 1
    assert snapshot[0].route_key == _MODEL_ID
    assert snapshot[0].canonical_model_id == _MODEL_ID
    assert snapshot[0].adapters == ((primary, 1.0),)
    assert route_table.canonical_id("contract-alias") == _MODEL_ID


@pytest.mark.unit
def test_base_router_is_not_public_or_in_serving_router_mro() -> None:
    import routing

    assert "BaseRouter" not in routing.__all__
    assert not hasattr(routing, "BaseRouter")
    assert routing.RouteTableRefreshable is RouteTableRefreshable
    assert routing.RouterProtocol is RouterProtocol
    assert all(base.__name__ != "BaseRouter" for base in FixedRouter.__mro__[1:])
    assert all(base.__name__ != "BaseRouter" for base in RouteWiseRouter.__mro__[1:])


def _routing_payloads(chunks: list[str]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for chunk in chunks:
        if not chunk.startswith("data: "):
            continue
        raw = chunk.removeprefix("data: ").strip()
        if raw == "[DONE]":
            continue
        payload = json.loads(raw)
        routing = payload.get("_routing")
        if isinstance(routing, dict):
            payloads.append(routing)
    return payloads


def _assert_primary_routing(routing: dict[str, Any]) -> None:
    assert routing["provider"] == "primary"
    assert routing["base_url"] == "https://primary.example/v1"
    assert routing["endpoint_id"] == f"{_MODEL_ID}:primary"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chat_success_routing_metadata_contract(router_factory: _RouterFactory) -> None:
    primary = _adapter("primary")
    router = router_factory.build([primary])

    response = await router.chat_completion(
        _MODEL_ID,
        _MESSAGES,
        request_id=f"contract-chat-{router_factory.name}",
    )

    assert response["choices"][0]["message"]["content"] == "ok"
    _assert_primary_routing(response["_routing"])
    assert primary.chat_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chat_error_attribution_contract(router_factory: _RouterFactory) -> None:
    primary = _adapter("primary", chat_error=RuntimeError("upstream failed"))
    router = router_factory.build([primary])

    with pytest.raises(RuntimeError, match="upstream failed") as exc_info:
        await router.chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id=f"contract-error-{router_factory.name}",
        )

    routing = getattr(exc_info.value, "_routing", None)
    assert routing is not None
    _assert_primary_routing(routing)
    assert routing["failed_attempts"] == [
        {
            "provider": "primary",
            "endpoint_id": f"{_MODEL_ID}:primary",
            "error_type": "RuntimeError",
            "error": "upstream failed",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_attempt_order_contract(
    router_factory: _RouterFactory,
) -> None:
    primary = _adapter(
        "primary",
        price="1",
        chat_error=ConnectionError("primary unavailable"),
    )
    backup = _adapter(
        "backup",
        price="100",
        chat_error=ConnectionError("backup unavailable"),
    )
    router = router_factory.build([primary, backup])

    with pytest.raises(ConnectionError) as exc_info:
        await router.chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id=f"contract-fallback-{router_factory.name}",
        )

    routing = getattr(exc_info.value, "_routing", None)
    assert routing is not None
    attempts = routing["failed_attempts"]
    assert [attempt["endpoint_id"] for attempt in attempts] == [
        f"{_MODEL_ID}:primary",
        f"{_MODEL_ID}:backup",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("chat", "stream"))
@pytest.mark.parametrize(
    ("status_code", "counts_as_failure"),
    ((400, False), (408, True), (429, True), (502, True)),
)
async def test_public_request_health_classification_contract(
    router_factory: _RouterFactory,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    status_code: int,
    counts_as_failure: bool,
) -> None:
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "10")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0")
    primary = _adapter(
        "primary",
        stream_chunks=('data: {"choices":[{"delta":{"content":"healthy"}}]}\n\n',),
    )
    router = router_factory.build([primary])
    request_id = f"contract-health-{router_factory.name}-{operation}-{status_code}"

    if operation == "chat":
        await router.chat_completion(_MODEL_ID, _MESSAGES, request_id=request_id)
    else:
        async for _ in router.stream_chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id=request_id,
        ):
            pass

    endpoint_id = f"{_MODEL_ID}:primary"
    baseline = router.get_provider_status()[endpoint_id]
    error = _StatusError(status_code)
    if operation == "chat":
        primary.chat_error = error
        call = router.chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id=f"{request_id}-failure",
        )
        with pytest.raises(_StatusError):
            await call
    else:
        primary.stream_chunks = ()
        primary.stream_error = error
        with pytest.raises(_StatusError):
            async for _ in router.stream_chat_completion(
                _MODEL_ID,
                _MESSAGES,
                request_id=f"{request_id}-failure",
            ):
                pass

    status = router.get_provider_status()[endpoint_id]
    assert status["circuit_state"] == "closed"
    if counts_as_failure:
        assert status["availability"] < baseline["availability"]
    else:
        assert status["availability"] == baseline["availability"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("successful_chunk", "improves_availability"),
    (
        ('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n', False),
        ('data: {"choices":[{"delta":{"content":"healthy"}}]}\n\n', True),
    ),
    ids=("role-only-does-not-reset", "content-resets"),
)
async def test_first_non_empty_stream_chunk_updates_health_contract(
    router_factory: _RouterFactory,
    monkeypatch: pytest.MonkeyPatch,
    successful_chunk: str,
    improves_availability: bool,
) -> None:
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "10")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0")
    primary = _adapter("primary")
    router = router_factory.build([primary])

    primary.stream_error = _StatusError(502)
    with pytest.raises(_StatusError):
        async for _ in router.stream_chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id=f"contract-success-boundary-{router_factory.name}-failure",
        ):
            pass

    endpoint_id = f"{_MODEL_ID}:primary"
    availability_after_failure = router.get_provider_status()[endpoint_id]["availability"]
    primary.stream_chunks = (successful_chunk,)
    primary.stream_error = None
    async for _ in router.stream_chat_completion(
        _MODEL_ID,
        _MESSAGES,
        request_id=f"contract-success-boundary-{router_factory.name}-success",
    ):
        pass

    availability_after_stream = router.get_provider_status()[endpoint_id]["availability"]
    if improves_availability:
        assert availability_after_stream > availability_after_failure
    else:
        assert availability_after_stream == availability_after_failure


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_routing_metadata_contract(router_factory: _RouterFactory) -> None:
    primary = _adapter(
        "primary",
        stream_chunks=(
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
            "data: [DONE]\n\n",
        ),
    )
    router = router_factory.build([primary])

    chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id=f"contract-stream-{router_factory.name}",
        )
    ]

    routing = _routing_payloads(chunks)[0]
    _assert_primary_routing(routing)
    assert "hello" in chunks[1]
    assert primary.stream_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_error_attribution_contract(router_factory: _RouterFactory) -> None:
    primary = _adapter("primary", stream_error=RuntimeError("stream failed"))
    router = router_factory.build([primary])

    with pytest.raises(RuntimeError, match="stream failed") as exc_info:
        async for _ in router.stream_chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id=f"contract-stream-error-{router_factory.name}",
        ):
            pass

    routing = getattr(exc_info.value, "_routing", None)
    assert routing is not None
    _assert_primary_routing(routing)
    assert [attempt["endpoint_id"] for attempt in routing["failed_attempts"]] == [
        f"{_MODEL_ID}:primary"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_falls_back_before_upstream_payload_contract(
    router_factory: _RouterFactory,
) -> None:
    primary = _adapter(
        "primary",
        price="1",
        stream_error=ConnectionError("failed before payload"),
    )
    backup = _adapter(
        "backup",
        price="100",
        stream_chunks=('data: {"choices":[{"delta":{"content":"backup"}}]}\n\n',),
    )
    router = router_factory.build([primary, backup])

    chunks = [
        chunk
        async for chunk in router.stream_chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id=f"contract-precommit-{router_factory.name}",
        )
    ]

    attributed = [routing for routing in _routing_payloads(chunks) if "provider" in routing]
    assert [routing["provider"] for routing in attributed] == ["primary", "backup"]
    assert attributed[-1]["failed_attempts"] == [
        {
            "provider": "primary",
            "endpoint_id": f"{_MODEL_ID}:primary",
            "error_type": "ConnectionError",
            "error": "failed before payload",
        }
    ]
    assert primary.stream_calls == 1
    assert backup.stream_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_does_not_fallback_after_upstream_payload_contract(
    router_factory: _RouterFactory,
) -> None:
    primary = _adapter(
        "primary",
        price="1",
        stream_chunks=('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',),
        stream_error=ConnectionError("failed after payload"),
    )
    backup = _adapter(
        "backup",
        price="100",
        stream_chunks=('data: {"choices":[{"delta":{"content":"spliced"}}]}\n\n',),
    )
    router = router_factory.build([primary, backup])
    chunks: list[str] = []

    with pytest.raises(ConnectionError, match="failed after payload"):
        async for chunk in router.stream_chat_completion(
            _MODEL_ID,
            _MESSAGES,
            request_id=f"contract-commit-{router_factory.name}",
        ):
            chunks.append(chunk)

    assert any('"role":"assistant"' in chunk for chunk in chunks)
    assert not any("spliced" in chunk for chunk in chunks)
    assert primary.stream_calls == 1
    assert backup.stream_calls == 0
