"""Regressions for a bound dispatch that outlives a route refresh. No network I/O.

Two things have to hold when the route table changes between the decision and the
dispatch: the I/O runs on the adapter that was bound, and it cannot borrow the
capacity of the adapter that replaced it. A binding this router cannot honor is
refused before any resource is committed or spent.
"""

from __future__ import annotations

import contextlib
import json
import time

import pytest

from routing.dispatch import DispatchMismatchError, binding_for_adapter
from routing.endpoint_health import _CircuitState
from routing.protocols import RoutingRequestOptions
from routing.routers import AllCircuitsOpenError, TargetUnavailableError
from tests.unit.routing import test_dispatch_contract as contract


async def _request(router, streaming, routing_options=None):
    if streaming:
        return [
            chunk
            async for chunk in router.stream_chat_completion(
                contract._MODEL_ID,
                contract._MESSAGES,
                routing_options=routing_options,
            )
        ]
    return await router.chat_completion(
        contract._MODEL_ID,
        contract._MESSAGES,
        routing_options=routing_options,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_fixed_releases_probe_claim_when_leaf_validation_refuses(streaming):
    """Refusal before upstream I/O must hand back the already acquired probe."""
    adapter = contract._adapter(
        contract._LOCAL_ENDPOINT, provider="local", base_url=contract._LOCAL_URL
    )
    adapter.reports_leg_outcomes = True
    router = contract._shared_router(adapter)
    registry = router._health_registry
    registry.ensure(contract._LOCAL_ENDPOINT)
    circuit = registry._circuits[contract._LOCAL_ENDPOINT]
    circuit.cooldown_seconds = 30
    circuit.state = _CircuitState.OPEN
    circuit.last_opened = time.perf_counter() - 31
    assert registry.allow_request(contract._LOCAL_ENDPOINT)

    with pytest.raises(DispatchMismatchError):
        await _request(router, streaming)

    assert adapter.chat_calls + adapter.stream_calls == 0
    assert registry.allow_request(contract._LOCAL_ENDPOINT), (
        "leaf refusal left the primary half-open probe occupied until its deadline"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_bound_execution_cannot_borrow_a_replacements_resource_pool(monkeypatch, streaming):
    """A refresh inside one router must not admit old-pool I/O using new-pool capacity."""
    original = contract._adapter(
        contract._CLOUD_ENDPOINT, provider="cloud", base_url="https://old.example/v1"
    )
    sibling = contract._adapter(
        contract._CHEAP_ENDPOINT, provider="cloud", base_url="https://old.example/v1"
    )
    replacement = contract._adapter(
        contract._CLOUD_ENDPOINT, provider="cloud", base_url="https://new.example/v1"
    )
    api = contract._adapter(
        contract._LOCAL_ENDPOINT, provider="local", base_url=contract._LOCAL_URL
    )
    for adapter, pool_id in (
        (original, "old-pool"),
        (sibling, "old-pool"),
        (replacement, "new-pool"),
    ):
        adapter.config.provider_type = "concurrency"
        adapter.config.concurrency_pool = pool_id
        adapter.config.concurrency = {"limit": 1}

    table = contract._shared_router(original, sibling, api)
    router = contract._routewise_router(table)
    binding = binding_for_adapter(original, model_id=contract._MODEL_ID)
    old_pool = router.concurrency_pools["old-pool"]
    assert old_pool.try_acquire(), "simulate an in-flight request filling the old account"
    table.register_route(contract._MODEL_ID, [(replacement, 1.0), (sibling, 1.0), (api, 1.0)])
    router.refresh_route_table()
    assert router.concurrency_pools["old-pool"] is old_pool
    new_pool = router.concurrency_pools["new-pool"]
    observed_during_io = []
    original_chat = original.chat_completion
    original_stream = original.stream_chat_completion

    async def record_chat(messages, **params):
        observed_during_io.append((old_pool.active, new_pool.active))
        return await original_chat(messages, **params)

    async def record_stream(messages, **params):
        observed_during_io.append((old_pool.active, new_pool.active))
        async for chunk in original_stream(messages, **params):
            yield chunk

    monkeypatch.setattr(original, "chat_completion", record_chat)
    monkeypatch.setattr(original, "stream_chat_completion", record_stream)
    options = RoutingRequestOptions(
        preferred_endpoint_id=contract._CLOUD_ENDPOINT,
        require_target=True,
        allow_fallback=False,
        bound_endpoint=binding,
    )
    try:
        with contextlib.suppress(
            DispatchMismatchError, TargetUnavailableError, AllCircuitsOpenError
        ):
            await _request(router, streaming, options)
        assert observed_during_io == [], (
            "old adapter was dispatched despite old-pool being full; "
            f"(old_pool.active, new_pool.active) during I/O = {observed_during_io}"
        )
    finally:
        old_pool.release()


@pytest.mark.asyncio
async def test_fixed_primary_stream_attributes_the_bound_adapter():
    """The stream metadata must describe the adapter that actually produced it."""
    original = contract._adapter(
        contract._CLOUD_ENDPOINT, provider="cloud", base_url="https://old.example/v1"
    )
    replacement = contract._adapter(
        contract._CLOUD_ENDPOINT, provider="cloud", base_url="https://new.example/v1"
    )
    router = contract._shared_router(original)
    binding = binding_for_adapter(original, model_id=contract._MODEL_ID)
    router.register_route(contract._MODEL_ID, [(replacement, 1.0)])
    chunks = await _request(
        router,
        True,
        RoutingRequestOptions(
            preferred_endpoint_id=contract._CLOUD_ENDPOINT,
            require_target=True,
            allow_fallback=False,
            bound_endpoint=binding,
        ),
    )
    assert (original.stream_calls, replacement.stream_calls) == (1, 0)
    metadata = json.loads(chunks[0].removeprefix("data: "))["_routing"]
    assert metadata["base_url"] == original.config.base_url
