"""Bindings must preserve pool preferences, re-solving, and execution attribution."""

from __future__ import annotations

import json
import time

import pytest

from routing.dispatch import binding_for_adapter
from routing.endpoint_health import _CircuitState
from routing.protocols import RoutingRequestOptions
from tests.unit.routing import test_dispatch_contract as contract


async def _request(router, streaming, options):
    if streaming:
        return [
            chunk
            async for chunk in router.stream_chat_completion(
                contract._MODEL_ID, contract._MESSAGES, routing_options=options
            )
        ]
    return await router.chat_completion(
        contract._MODEL_ID, contract._MESSAGES, routing_options=options
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("primary_state", ["busy", "fails"])
async def test_routewise_preference_allows_another_endpoint(streaming, primary_state):
    """A pool may replace a soft preference before I/O or after a retryable failure."""
    failure = (
        ConnectionError("preferred endpoint disconnected") if primary_state == "fails" else None
    )
    preferred = contract._adapter(
        contract._LOCAL_ENDPOINT,
        provider="local",
        base_url=contract._LOCAL_URL,
        chat_error=failure,
        stream_error=failure,
    )
    preferred.config.provider_type = "concurrency"
    preferred.config.concurrency_pool = "preferred-pool"
    preferred.config.concurrency = {"limit": 1}
    alternative = contract._adapter(
        contract._CLOUD_ENDPOINT, provider="cloud", base_url="https://alternative.example/v1"
    )
    router = contract._routewise_router(contract._shared_router(preferred, alternative))
    pool = router.concurrency_pools["preferred-pool"]
    if primary_state == "busy":
        assert pool.try_acquire()
    options = RoutingRequestOptions(
        preferred_endpoint_id=contract._LOCAL_ENDPOINT,
        require_target=False,
        allow_fallback=True,
        bound_endpoint=binding_for_adapter(preferred, model_id=contract._MODEL_ID),
    )
    try:
        try:
            await _request(router, streaming, options)
        except Exception as exc:
            pytest.fail(
                f"legal pool substitution was refused: {type(exc).__name__}: {exc}; "
                f"preferred calls={preferred.chat_calls + preferred.stream_calls}, "
                f"alternative calls={alternative.chat_calls + alternative.stream_calls}"
            )
        assert preferred.chat_calls + preferred.stream_calls == (primary_state == "fails")
        assert alternative.chat_calls + alternative.stream_calls == 1
    finally:
        if primary_state == "busy":
            pool.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_fixed_recovered_preference_has_bound_fallback_attribution(monkeypatch, streaming):
    """A temporarily unavailable preference may recover during another attempt."""
    original = contract._adapter(
        contract._CLOUD_ENDPOINT, provider="cloud", base_url="https://old.example/v1"
    )
    replacement = contract._adapter(
        contract._CLOUD_ENDPOINT, provider="cloud", base_url="https://new.example/v1"
    )
    primary = contract._adapter(
        contract._LOCAL_ENDPOINT,
        provider="local",
        base_url=contract._LOCAL_URL,
        chat_error=ConnectionError("primary disconnected"),
        stream_error=ConnectionError("primary disconnected"),
    )
    router = contract._shared_router(original, primary)
    binding = binding_for_adapter(original, model_id=contract._MODEL_ID)
    router.register_route(contract._MODEL_ID, [(replacement, 1.0), (primary, 1.0)])
    registry = router._health_registry
    registry.ensure(contract._CLOUD_ENDPOINT)
    circuit = registry._circuits[contract._CLOUD_ENDPOINT]
    circuit.cooldown_seconds = 30
    circuit.state = _CircuitState.OPEN
    circuit.last_opened = time.perf_counter()
    assert not registry.allow_request(contract._CLOUD_ENDPOINT)

    original_chat = primary.chat_completion
    original_stream = primary.stream_chat_completion

    async def chat_after_cooldown(messages, **params):
        circuit.last_opened = time.perf_counter() - 31
        return await original_chat(messages, **params)

    async def stream_after_cooldown(messages, **params):
        circuit.last_opened = time.perf_counter() - 31
        async for chunk in original_stream(messages, **params):
            yield chunk

    monkeypatch.setattr(primary, "chat_completion", chat_after_cooldown)
    monkeypatch.setattr(primary, "stream_chat_completion", stream_after_cooldown)
    result = await _request(
        router,
        streaming,
        RoutingRequestOptions(
            preferred_endpoint_id=contract._CLOUD_ENDPOINT,
            require_target=False,
            allow_fallback=True,
            bound_endpoint=binding,
        ),
    )
    assert primary.chat_calls + primary.stream_calls == 1
    assert original.chat_calls + original.stream_calls == 1
    assert replacement.chat_calls + replacement.stream_calls == 0
    if streaming:
        blocks = [json.loads(chunk.removeprefix("data: ")) for chunk in result]
        routing = [block["_routing"] for block in blocks if "_routing" in block][-1]
    else:
        routing = result["_routing"]
    assert routing["base_url"] == original.config.base_url
