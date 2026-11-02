"""The "nothing admitted" error names every endpoint with the reason it was passed over.

Production incident shape: ``glm-5.3-flash`` has an image-capable cloud route
(zai), two text-only routes (a local vLLM server and ollama) and an
image-capable staging canary at weight 0. An image request with zai's breaker
open failed with "All provider circuits are open ... ['...:zai-api',
'...:staging-api']": the muted canary was reported as an open circuit, and the
local server -- filtered out for modality -- was not mentioned at all, so it
read as missing. The error type and its 503 disposition are a contract callers
and alerting key on; only the message changes.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from routing.protocols import RoutingRequestOptions
from routing.routers import AllCircuitsOpenError, FixedRouter, TargetUnavailableError
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "glm-5.3-flash"
_ZAI = f"{_MODEL_ID}:zai-api"
_LOCAL = f"{_MODEL_ID}:local-8200"
_OLLAMA = f"{_MODEL_ID}:local-11434"
_STAGING = f"{_MODEL_ID}:staging-api"
_LOCAL_POOL = "local-pool"
_MESSAGES = [{"role": "user", "content": "hello"}]
_IMAGE = RoutingRequestOptions(required_modalities=frozenset({"image"}))
_TEXT_AND_IMAGE = ["text", "image"]


class _RecordingAdapter(BaseAdapter):
    """Adapter that counts calls; none of these tests may reach one."""

    def __init__(self, config: ModelConfig) -> None:
        super().__init__(config)
        self.calls = 0

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        self.calls += 1
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self.calls += 1
        yield "data: [DONE]\n\n"


def _adapter(
    endpoint_id: str,
    *,
    provider: str,
    input_modalities: list[str] | None = None,
    **config: Any,
) -> _RecordingAdapter:
    return _RecordingAdapter(
        ModelConfig(
            id=_MODEL_ID,
            name=_MODEL_ID,
            provider=provider,
            base_url=f"https://{provider}.example/v1",
            endpoint_id=endpoint_id,
            pricing={"prompt": "1", "completion": "1"},
            input_modalities=input_modalities or ["text"],
            **config,
        )
    )


def _open_circuit(registry: EndpointHealthRegistry, endpoint_id: str) -> None:
    registry.ensure(endpoint_id)
    circuit = registry._circuits[endpoint_id]
    circuit.state = _CircuitState.OPEN
    circuit.last_opened = time.perf_counter()


class _DisabledProviders:
    def __init__(self, *providers: str) -> None:
        self._disabled = set(providers)

    def is_disabled(self, provider: str) -> bool:
        return provider in self._disabled


# ---------------------------------------------------------------------------
# FixedRouter
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("muted_by", ["configured_weight", "admin_disabled"])
async def test_fixed_reports_a_weight_0_route_apart_from_an_open_circuit(muted_by: str) -> None:
    zai = _adapter(_ZAI, provider="zai")
    staging = _adapter(_STAGING, provider="staging")
    router = FixedRouter()
    if muted_by == "admin_disabled":
        router.register_route(_MODEL_ID, [(zai, 1.0), (staging, 1.0)])
        router.disabled_provider_resolver = _DisabledProviders("staging")
    else:
        router.register_route(_MODEL_ID, [(zai, 1.0), (staging, 0.0)])
    _open_circuit(router.endpoint_health_registry, _ZAI)

    with pytest.raises(AllCircuitsOpenError) as raised:
        await router.chat_completion(_MODEL_ID, _MESSAGES)

    assert str(raised.value) == (
        f"No admissible route for model {_MODEL_ID}: {_ZAI}=circuit_open, {_STAGING}=weight_0"
    )
    assert zai.calls == 0
    assert staging.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fixed_never_admits_a_nan_weight() -> None:
    """``weight <= 0`` is false for NaN; selection must keep refusing it anyway."""
    zai = _adapter(_ZAI, provider="zai")
    staging = _adapter(_STAGING, provider="staging")
    router = FixedRouter()
    # A NaN anywhere makes the normalizing sum NaN, so every weight becomes NaN.
    router.register_route(_MODEL_ID, [(zai, float("nan")), (staging, 0.0)])

    with pytest.raises(AllCircuitsOpenError) as raised:
        await router.chat_completion(_MODEL_ID, _MESSAGES)

    assert str(raised.value) == (
        f"No admissible route for model {_MODEL_ID}: {_ZAI}=weight_0, {_STAGING}=weight_0"
    )
    assert zai.calls == 0
    assert staging.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fixed_image_request_names_the_text_only_routes_it_filtered_out() -> None:
    zai = _adapter(_ZAI, provider="zai", input_modalities=_TEXT_AND_IMAGE)
    local = _adapter(_LOCAL, provider="local")
    ollama = _adapter(_OLLAMA, provider="ollama")
    staging = _adapter(_STAGING, provider="staging", input_modalities=_TEXT_AND_IMAGE)
    router = FixedRouter()
    router.register_route(_MODEL_ID, [(zai, 1.0), (local, 1.0), (ollama, 1.0), (staging, 0.0)])
    _open_circuit(router.endpoint_health_registry, _ZAI)

    with pytest.raises(AllCircuitsOpenError) as raised:
        await router.chat_completion(_MODEL_ID, _MESSAGES, routing_options=_IMAGE)

    assert str(raised.value) == (
        f"No admissible route for model {_MODEL_ID}: {_ZAI}=circuit_open, {_STAGING}=weight_0; "
        f"filtered out for input modalities ['image']: {_LOCAL}, {_OLLAMA}"
    )
    assert [adapter.calls for adapter in (zai, local, ollama, staging)] == [0, 0, 0, 0]

    # The same route still serves text: the local server was filtered, not lost.
    response = await router.chat_completion(_MODEL_ID, _MESSAGES)
    assert response["_routing"]["endpoint_id"] in {_LOCAL, _OLLAMA}


@pytest.mark.unit
def test_fixed_reports_an_endpoint_this_request_already_passed_over_as_excluded() -> None:
    zai = _adapter(_ZAI, provider="zai")
    local = _adapter(_LOCAL, provider="local")
    router = FixedRouter()
    router.register_route(_MODEL_ID, [(zai, 1.0), (local, 1.0)])
    _open_circuit(router.endpoint_health_registry, _ZAI)

    with pytest.raises(AllCircuitsOpenError) as raised:
        router._select_adapter(_MODEL_ID, exclude={_LOCAL})

    assert str(raised.value) == (
        f"No admissible route for model {_MODEL_ID}: {_ZAI}=circuit_open, {_LOCAL}=excluded"
    )


@pytest.mark.unit
def test_fixed_no_route_for_the_modality_names_the_routes_it_filtered_out() -> None:
    router = FixedRouter()
    router.register_route(
        _MODEL_ID,
        [(_adapter(_LOCAL, provider="local"), 1.0), (_adapter(_OLLAMA, provider="ollama"), 1.0)],
    )

    with pytest.raises(AllCircuitsOpenError) as raised:
        router._select_adapter(_MODEL_ID, required_modalities=frozenset({"image"}))

    assert str(raised.value) == (
        f"No route for model {_MODEL_ID} accepts input modalities ['image']: "
        f"filtered out {_LOCAL}, {_OLLAMA}"
    )


# ---------------------------------------------------------------------------
# RouteWiseRouter
# ---------------------------------------------------------------------------


def _routewise(adapters: list[_RecordingAdapter]) -> RouteWiseRouter:
    table = FixedRouter()
    table.register_route(_MODEL_ID, [(adapter, 1.0) for adapter in adapters])
    return RouteWiseRouter(
        route_table=table,
        config=RouteWiseConfig(budget_alpha=0.0, fallback_mode="policy", random_seed=0),
    )


def _busy_local() -> _RecordingAdapter:
    return _adapter(
        _LOCAL,
        provider="local",
        provider_type="concurrency",
        concurrency={"limit": 1},
        concurrency_pool=_LOCAL_POOL,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_routewise_open_circuit_does_not_hide_a_capacity_refused_local() -> None:
    local = _busy_local()
    zai = _adapter(_ZAI, provider="zai")
    router = _routewise([local, zai])
    pool = router.concurrency_pools[_LOCAL_POOL]
    assert pool.try_acquire()
    _open_circuit(router._health_registry, _ZAI)

    try:
        with pytest.raises(AllCircuitsOpenError) as raised:
            await router.chat_completion(_MODEL_ID, _MESSAGES)
    finally:
        pool.release()

    assert str(raised.value) == (
        f"No admissible route for model {_MODEL_ID}: {_LOCAL}=no_capacity, {_ZAI}=circuit_open"
    )
    assert local.calls == 0
    assert zai.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_routewise_capacity_only_keeps_its_disposition_and_names_the_filtered_routes() -> (
    None
):
    local = _adapter(
        _LOCAL,
        provider="local",
        input_modalities=_TEXT_AND_IMAGE,
        provider_type="concurrency",
        concurrency={"limit": 1},
        concurrency_pool=_LOCAL_POOL,
    )
    ollama = _adapter(_OLLAMA, provider="ollama")
    router = _routewise([local, ollama])
    pool = router.concurrency_pools[_LOCAL_POOL]
    assert pool.try_acquire()

    try:
        with pytest.raises(TargetUnavailableError) as raised:
            await router.chat_completion(_MODEL_ID, _MESSAGES, routing_options=_IMAGE)
    finally:
        pool.release()

    assert str(raised.value) == (
        f"no candidate for model {_MODEL_ID} has capacity right now: {_LOCAL}=no_capacity; "
        f"filtered out for input modalities ['image']: {_OLLAMA}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_routewise_image_request_names_the_text_only_routes_it_filtered_out() -> None:
    zai = _adapter(_ZAI, provider="zai", input_modalities=_TEXT_AND_IMAGE)
    local = _adapter(_LOCAL, provider="local")
    ollama = _adapter(_OLLAMA, provider="ollama")
    router = _routewise([zai, local, ollama])
    _open_circuit(router._health_registry, _ZAI)

    with pytest.raises(AllCircuitsOpenError) as raised:
        await router.chat_completion(_MODEL_ID, _MESSAGES, routing_options=_IMAGE)

    assert str(raised.value) == (
        f"No admissible route for model {_MODEL_ID}: {_ZAI}=circuit_open; "
        f"filtered out for input modalities ['image']: {_LOCAL}, {_OLLAMA}"
    )
    assert [adapter.calls for adapter in (zai, local, ollama)] == [0, 0, 0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_routewise_no_route_for_the_modality_names_the_routes_it_filtered_out() -> None:
    router = _routewise([_adapter(_LOCAL, provider="local"), _adapter(_OLLAMA, provider="ollama")])

    with pytest.raises(AllCircuitsOpenError) as raised:
        await router.chat_completion(_MODEL_ID, _MESSAGES, routing_options=_IMAGE)

    assert str(raised.value) == (
        f"No route for model {_MODEL_ID} accepts input modalities ['image']: "
        f"filtered out {_LOCAL}, {_OLLAMA}"
    )
