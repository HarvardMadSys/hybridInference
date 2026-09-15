"""Regressions for the leaf execution boundary: bindings and early refusal.

A resolved binding has to reach the execution path, not just the instruction
check, and a binding this router cannot honor has to be refused before any
resource is committed -- a released reservation does not refund spent quota.
No network I/O.
"""

from __future__ import annotations

import pytest

from routing.backends import FixedCloudBackend, LocalBackend
from routing.dispatch import DispatchMismatchError, ExecuteEndpoint
from routing.hybrid import HybridRouter
from tests.unit.routing import (
    test_dispatch_contract as contract,
    test_routewise_router as routewise_tests,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_hybrid_executes_the_adapter_in_its_checked_binding(monkeypatch, streaming):
    """Refresh between instruction validation and dispatch must not replace a binding."""
    local = contract._adapter(
        contract._LOCAL_ENDPOINT,
        provider="local",
        base_url=contract._LOCAL_URL,
        chat_error=RuntimeError("local failed"),
        stream_error=RuntimeError("local failed"),
    )
    original = contract._adapter(
        contract._CLOUD_ENDPOINT, provider="cloud", base_url="https://old.example/v1"
    )
    replacement = contract._adapter(
        contract._CLOUD_ENDPOINT, provider="cloud", base_url="https://new.example/v1"
    )
    shared = contract._shared_router(local, original)
    cloud = FixedCloudBackend(
        shared,
        endpoint_scope={contract._CLOUD_ENDPOINT},
        model_scope={contract._MODEL_ID},
    )
    checked_bindings = []
    original_check = cloud.check_instruction

    def check_then_refresh(instruction, model_id):
        original_check(instruction, model_id)
        if isinstance(instruction, ExecuteEndpoint):
            checked_bindings.append(instruction.binding)
            shared.register_route(model_id, [(local, 1.0), (replacement, 1.0)])

    monkeypatch.setattr(cloud, "check_instruction", check_then_refresh)
    router = HybridRouter(
        policy=contract._PlannedPolicy(
            primary="local",
            primary_endpoint=contract._LOCAL_ENDPOINT,
            fallback_endpoint=contract._CLOUD_ENDPOINT,
        ),
        local=LocalBackend(
            shared,
            endpoint_scope={contract._LOCAL_ENDPOINT},
            model_scope={contract._MODEL_ID},
        ),
        cloud=cloud,
    )
    if streaming:
        _ = [
            chunk
            async for chunk in router.stream_chat_completion(contract._MODEL_ID, contract._MESSAGES)
        ]
    else:
        await router.chat_completion(contract._MODEL_ID, contract._MESSAGES)

    assert len(checked_bindings) == 1
    assert checked_bindings[0].adapter is original
    assert (
        original.chat_calls + original.stream_calls,
        replacement.chat_calls + replacement.stream_calls,
    ) == (1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_fixed_does_not_record_leaf_rejection_as_provider_failure(streaming):
    """A composite is rejected before I/O and must not create a provider sample."""
    adapter = contract._adapter(
        contract._LOCAL_ENDPOINT, provider="local", base_url=contract._LOCAL_URL
    )
    adapter.reports_leg_outcomes = True
    router = contract._shared_router(adapter)
    with pytest.raises(DispatchMismatchError) as raised:
        if streaming:
            _ = [
                chunk
                async for chunk in router.stream_chat_completion(
                    contract._MODEL_ID, contract._MESSAGES
                )
            ]
        else:
            await router.chat_completion(contract._MODEL_ID, contract._MESSAGES)

    assert adapter.chat_calls + adapter.stream_calls == 0
    assert not getattr(raised.value, "_routing", {}).get("failed_attempts")


def test_routewise_rejects_composite_before_consuming_quota():
    """A reservation release does not refund quota already consumed by commit."""
    router, quota_adapter, api_adapter = routewise_tests._make_router_with_quota_and_api(
        prompt_price="3.0", completion_price="15.0"
    )
    routewise_tests._warm_envelope(router, lower=0.0000001, upper=0.001)
    for _ in range(25):
        router.predictor.update("test-model", 500)
    quota_adapter.reports_leg_outcomes = True
    pool = routewise_tests._quota_pool(router)
    remaining_before = pool.remaining

    with pytest.raises(DispatchMismatchError):
        router._select_decision("test-model", {"prompt_tokens": 1000})

    quota_adapter.chat_completion.assert_not_called()
    api_adapter.chat_completion.assert_not_called()
    assert pool.remaining == remaining_before
