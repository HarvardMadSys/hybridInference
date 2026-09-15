"""Verify outstanding PR findings against the final proposed merge commit."""

from __future__ import annotations

import contextlib
import json

import pytest

from routing.backends import FixedCloudBackend, LocalBackend
from routing.decisions import RoutingTarget
from routing.dispatch import DispatchMismatchError, EndpointBinding
from routing.endpoint_health import EndpointHealthRegistry
from routing.hybrid import HybridRouter
from routing.protocols import RoutingRequestOptions
from routing.routers import FixedRouter, ProviderPinError, TargetUnavailableError
from serving.adapters.base import ModelConfig
from tests.unit.routing import (
    test_dispatch_contract as contract,
    test_hybrid_router as hybrid_tests,
)


def _adapter(model_id, endpoint_id, *, provider="shared-provider"):
    return contract._RecordingAdapter(
        ModelConfig(
            id=model_id,
            name=model_id,
            provider=provider,
            base_url=f"https://{model_id}.example/v1",
            endpoint_id=endpoint_id,
            pricing={"prompt": "1", "completion": "1"},
            input_modalities=["text"],
        )
    )


def _table(*adapters):
    router = FixedRouter(health_registry=EndpointHealthRegistry())
    by_model = {}
    for adapter in adapters:
        by_model.setdefault(adapter.config.id, []).append((adapter, 1.0))
    for model_id, candidates in by_model.items():
        router.register_route(model_id, candidates)
    return router


async def _request(router, model_id, streaming, options=None, **params):
    if streaming:
        return [
            chunk
            async for chunk in router.stream_chat_completion(
                model_id, contract._MESSAGES, routing_options=options, **params
            )
        ]
    return await router.chat_completion(
        model_id, contract._MESSAGES, routing_options=options, **params
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_hybrid_binding_lookup_stays_in_the_requested_model(streaming):
    """Operational route IDs are unique per model, not across all models."""
    a = _adapter("model-a", "shared-route-id")
    b = _adapter("model-b", "shared-route-id")
    local = _adapter("model-b", "model-b:local", provider="local")
    table = _table(a, b, local)
    router = HybridRouter(
        policy=hybrid_tests._ForceBackend(
            "cloud", target=RoutingTarget(endpoint_id="shared-route-id")
        ),
        local=LocalBackend(table, endpoint_scope={"model-b:local"}, model_scope={"model-b"}),
        cloud=FixedCloudBackend(table, endpoint_scope={"shared-route-id"}, model_scope={"model-b"}),
    )
    await _request(router, "model-b", streaming)
    assert (a.chat_calls + a.stream_calls, b.chat_calls + b.stream_calls) == (0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("backend_type", [LocalBackend, FixedCloudBackend])
async def test_backend_refuses_a_model_outside_its_declared_scope(streaming, backend_type):
    """A provider label shared across models cannot widen the model grant."""
    a = _adapter("model-a", "model-a:cloud")
    b = _adapter("model-b", "model-b:cloud")
    backend = backend_type(
        _table(a, b), endpoint_scope={"shared-provider"}, model_scope={"model-a"}
    )
    with contextlib.suppress(DispatchMismatchError, ProviderPinError, TargetUnavailableError):
        await _request(backend, "model-b", streaming)
    assert b.chat_calls + b.stream_calls == 0, "the backend served an excluded model"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_hard_pin_cannot_escape_the_backend_endpoint_scope(streaming):
    """Bypassing a health circuit does not grant access to another backend's endpoint."""
    local = _adapter("model-b", "model-b:local", provider="local")
    cloud = _adapter("model-b", "model-b:cloud", provider="cloud")
    backend = LocalBackend(
        _table(local, cloud), endpoint_scope={"model-b:local"}, model_scope={"model-b"}
    )
    with contextlib.suppress(DispatchMismatchError, ProviderPinError, TargetUnavailableError):
        await _request(backend, "model-b", streaming, RoutingRequestOptions(pin_provider="cloud"))
    assert cloud.chat_calls + cloud.stream_calls == 0, "the local pool dispatched to cloud"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_inconsistent_binding_cannot_execute_a_different_endpoint(streaming):
    """An invalid binding must be refused before its unrelated adapter runs."""
    selected = _adapter("model-b", "model-b:selected")
    other = _adapter("model-b", "model-b:other")
    table = _table(selected, other)
    with contextlib.suppress(ValueError, DispatchMismatchError):
        binding = EndpointBinding(endpoint_id="model-b:selected", model_id="model-b", adapter=other)
        await _request(
            table,
            "model-b",
            streaming,
            RoutingRequestOptions(
                preferred_endpoint_id="model-b:selected",
                bound_endpoint=binding,
                require_target=True,
                allow_fallback=False,
            ),
        )
    assert other.chat_calls + other.stream_calls == 0, "binding endpoint and actual I/O differ"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_owner", ["local", "none"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_feedback_keeps_the_backend_that_executed_before_refresh(new_owner, streaming):
    """Moving/removing an endpoint does not move ownership of an earlier request."""
    local = hybrid_tests._CountingBackend("local", owns=False)
    cloud = hybrid_tests._CountingBackend("cloud", owns=True)
    router = HybridRouter(policy=hybrid_tests._ForceBackend("cloud"), local=local, cloud=cloud)
    request_id = "request-before-refresh"
    await _request(router, hybrid_tests._MODEL_ID, streaming, request_id=request_id)
    assert cloud.targets_seen == [None], "the cloud backend actually handled the request"
    cloud.owns = False
    local.owns = new_owner == "local"
    router.record_observation(
        hybrid_tests._observation(hybrid_tests._CLOUD_ENDPOINT, request_id=request_id)
    )
    assert (cloud.observations, local.observations) == ([hybrid_tests._CLOUD_ENDPOINT], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_each_attempt_keeps_feedback_after_both_scopes_change(monkeypatch, streaming):
    """The failed local attempt and cloud answer keep distinct owners after refresh."""
    local = hybrid_tests._CountingBackend("local", owns=True)
    cloud = hybrid_tests._CountingBackend("cloud", owns=True)
    router = HybridRouter(
        policy=hybrid_tests._ForceBackend("local", fallbacks=("cloud",)),
        local=local,
        cloud=cloud,
    )
    failure = ConnectionError("local failed")
    failure._routing = {"endpoint_id": hybrid_tests._LOCAL_ENDPOINT}
    response = {"choices": [], "_routing": {"endpoint_id": hybrid_tests._CLOUD_ENDPOINT}}

    async def fail_chat(*args, **kwargs):
        raise failure

    async def succeed_chat(*args, **kwargs):
        return response

    async def fail_stream(*args, **kwargs):
        raise failure
        yield  # pragma: no cover

    async def succeed_stream(*args, **kwargs):
        yield f"data: {json.dumps(response)}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(local, "chat_completion", fail_chat)
    monkeypatch.setattr(cloud, "chat_completion", succeed_chat)
    monkeypatch.setattr(local, "stream_chat_completion", fail_stream)
    monkeypatch.setattr(cloud, "stream_chat_completion", succeed_stream)
    await _request(router, hybrid_tests._MODEL_ID, streaming, request_id="two-attempts")
    local.owns = cloud.owns = False
    router.record_observation(
        hybrid_tests._observation(
            hybrid_tests._LOCAL_ENDPOINT, request_id="two-attempts", terminal=False, success=False
        )
    )
    router.record_observation(
        hybrid_tests._observation(hybrid_tests._CLOUD_ENDPOINT, request_id="two-attempts")
    )
    assert local.observations == [hybrid_tests._LOCAL_ENDPOINT]
    assert cloud.observations == [hybrid_tests._CLOUD_ENDPOINT]
    assert "two-attempts" not in router._backend_decisions


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_model_scoped_binding_accepts_an_alias(streaming):
    """Canonical model lookup must preserve the public alias entry point."""
    adapter = _adapter("model-b", "model-b:cloud")
    table = _table(adapter)
    table.register_route("model-b", [(adapter, 1.0)], aliases=["alias-b"])
    backend = FixedCloudBackend(table, endpoint_scope={"shared-provider"}, model_scope={"model-b"})
    binding = backend.binding_for("alias-b", "model-b:cloud")
    assert binding is not None and binding.adapter is adapter
    await _request(backend, "alias-b", streaming)
    assert adapter.chat_calls + adapter.stream_calls == 1


@pytest.mark.asyncio
async def test_fallback_feedback_uses_the_executing_backend_not_the_initial_policy(monkeypatch):
    """Dispatch ownership must track the actual fallback, including overlapping claims."""
    local = hybrid_tests._CountingBackend("local", owns=True)
    cloud = hybrid_tests._CountingBackend("cloud", owns=True)

    async def fail_primary(*args, **kwargs):
        raise ConnectionError("local failed before cloud fallback")

    monkeypatch.setattr(local, "chat_completion", fail_primary)
    router = HybridRouter(
        policy=hybrid_tests._ForceBackend("local", fallbacks=("cloud",)),
        local=local,
        cloud=cloud,
    )
    request_id = "request-served-by-fallback"
    await router.chat_completion(
        hybrid_tests._MODEL_ID, hybrid_tests._MESSAGES, request_id=request_id
    )
    assert cloud.targets_seen == [None]
    router.record_observation(
        hybrid_tests._observation(hybrid_tests._CLOUD_ENDPOINT, request_id=request_id)
    )
    assert (cloud.observations, local.observations) == ([hybrid_tests._CLOUD_ENDPOINT], [])
