"""Experiment-local baseline admission, resource ownership and stream lifecycle."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import pytest

from benchmark.nimbus.baselines import BaselineProfile, BaselineSnapshot
from benchmark.nimbus.registry import BaselineModelRouterRegistry
from benchmark.nimbus.router import BaselinePoolRegistry, BaselineRouter
from routing.dependencies import RouterBuildDependencies
from routing.dispatch import DispatchMismatchError, binding_for_adapter
from routing.endpoint_health import EndpointHealthRegistry
from routing.model_router_registry import ModelRouterRegistry
from routing.protocols import RouterProtocol, RoutingRequestOptions
from routing.routers import FixedRouter, TargetUnavailableError
from routing.strategies import validate_router_config
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.utils import context as req_ctx

MESSAGES = [{"role": "user", "content": "test prompt " * 20}]


def _chunk(delta):
    return f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n"


class _Adapter(BaseAdapter):
    def __init__(self, endpoint, *, block=False, fail_after_content=False):
        super().__init__(
            ModelConfig(
                id="model",
                name="model",
                provider=endpoint,
                endpoint_id=endpoint,
                base_url=f"http://{endpoint}.invalid",
            )
        )
        self.calls = []
        self.closed = 0
        self.release = asyncio.Event()
        self.fail_after_content = fail_after_content
        if not block:
            self.release.set()

    async def chat_completion(self, messages, **params):
        self.calls.append((dict(req_ctx.get()), dict(params)))
        try:
            await self.release.wait()
            return {"choices": [{"message": {"content": self.config.provider}}]}
        finally:
            self.closed += 1

    async def stream_chat_completion(self, messages, **params):
        self.calls.append((dict(req_ctx.get()), dict(params)))
        try:
            yield _chunk({"role": "assistant"})
            await self.release.wait()
            yield _chunk({"reasoning_content": "thinking"})
            if self.fail_after_content:
                raise RuntimeError("upstream failed after visible output")
            yield _chunk({"content": self.config.provider})
            yield "data: [DONE]\n\n"
        finally:
            self.closed += 1


def _config(policy="greedy", **changes):
    profile = BaselineProfile(256, 100, 10_000, 0.001, 0, 10, 0)
    return {
        "local_endpoint_id": "local",
        "cloud_endpoint_id": "api",
        "pool_id": "gpu-pool",
        "policy": policy,
        "profile": asdict(profile),
        "local_max_inflight": 1,
        "remote_input_cost_per_million": 1,
        "remote_output_cost_per_million": 3,
        "batch_window_s": 0.01,
        "cancel_grace_s": 0,
        **changes,
    }


def _build(*, config=None, local=None, cloud=None, pools=None, sink=None):
    health = EndpointHealthRegistry()
    fixed = FixedRouter(health_registry=health)
    local = local or _Adapter("local")
    cloud = cloud or _Adapter("api")
    fixed.register_route("model", [(local, 1), (cloud, 1)], aliases=["alias"])
    pools = pools or BaselinePoolRegistry()
    records = []
    registry = BaselineModelRouterRegistry(
        {"model": {"router": "baseline_experiment", "router_params": config or _config()}},
        alias_to_model={"alias": "model"},
        shared_fixed_router=fixed,
        dependencies=RouterBuildDependencies(health_registry=health),
        baseline_pools=pools,
        baseline_decision_sink=records.append if sink is None else sink,
    )
    router = registry.get_router("model")
    return router, local, cloud, records, registry, fixed


async def _wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), 2)


def test_registry_builds_real_peer_strategy_and_aliases_share_owner():
    router, _, _, _, registry, fixed = _build()
    assert isinstance(router, BaselineRouter)
    assert isinstance(router, RouterProtocol)
    assert registry.get_router("alias") is router
    assert registry.get_router_name("model") == "baseline_experiment"
    assert registry.get_router("unconfigured-model") is fixed
    assert registry.managed_routers() == [router]


def test_registry_refuses_to_construct_private_capacity_owner():
    fixed = FixedRouter()
    registry = BaselineModelRouterRegistry(
        {"model": {"router": "baseline_experiment", "router_params": _config()}},
        shared_fixed_router=fixed,
    )
    with pytest.raises(ValueError, match="explicitly shared"):
        registry.get_router("model")


@pytest.mark.parametrize("name", ["nimbus", "baseline_experiment"])
def test_experiment_names_are_not_production_strategies(name):
    with pytest.raises(ValueError, match="unknown router"):
        validate_router_config(name, {})
    registry = ModelRouterRegistry(
        {"model": {"router": name, "router_params": _config()}},
        shared_fixed_router=FixedRouter(),
    )
    with pytest.raises(ValueError, match="unknown router"):
        registry.get_router("model")


@pytest.mark.parametrize(
    ("policy", "expected_local"),
    [
        ("all_local", 3),
        ("all_api", 0),
        ("concurrency", 1),
        ("greedy", 1),
        ("rolling_knapsack_v1", 1),
    ],
)
async def test_all_baselines_and_policies_run_through_same_registry_path(policy, expected_local):
    router, local, cloud, records, _, _ = _build(
        config=_config(policy),
        local=_Adapter("local", block=True),
        cloud=_Adapter("api", block=True),
    )
    tasks = [
        asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
        for _ in range(3)
    ]
    await _wait_until(lambda: len(records) == 3)
    await _wait_until(
        lambda: len(local.calls) + len(cloud.calls) == min(expected_local, 1) + 3 - expected_local
    )
    assert len(local.calls) == min(expected_local, 1)
    assert len(cloud.calls) == 3 - expected_local
    assert len(records) == 3
    assert {record["decision_start_s"] for record in records}.__len__() == 1
    assert all(record["decision_end_s"] >= record["decision_start_s"] for record in records)
    local.release.set()
    cloud.release.set()
    responses = await asyncio.gather(*tasks)
    assert len(local.calls) == expected_local
    assert all(
        response["_routing"]["strategy_metadata"]["baseline_experiment"] for response in responses
    )
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_batch_does_not_execute_adapter_under_first_callers_context():
    router, local, cloud, records, _, _ = _build()

    async def call(request_id):
        with req_ctx.push(request_id=request_id):
            return await router.chat_completion("model", MESSAGES, max_tokens=100)

    responses = await asyncio.gather(call("one"), call("two"))
    seen = {call[0]["request_id"] for call in [*local.calls, *cloud.calls]}
    assert seen == {"one", "two"}
    assert {record["request_id"] for record in records} == seen
    assert {
        response["_routing"]["strategy_metadata"]["baseline_experiment"]["request_id"]
        for response in responses
    } == seen


async def test_pending_cancellation_never_calls_an_adapter_or_reserves_capacity():
    router, local, cloud, records, _, _ = _build(config=_config(batch_window_s=0.05))
    task = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: bool(router._pending))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.06)
    assert not local.calls and not cloud.calls and not records
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_cancel_during_shared_pool_lock_wait_does_not_leak_reservation():
    router, local, cloud, _, _, _ = _build(config=_config(batch_window_s=0))
    await router._pool.lock.acquire()
    task = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await asyncio.sleep(0.01)
    task.cancel()
    router._pool.lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not local.calls and not cloud.calls
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_abandoning_stream_after_routing_metadata_never_sends_provider_request():
    router, local, cloud, _, _, _ = _build()
    stream = router.stream_chat_completion("model", MESSAGES, max_tokens=100)
    metadata = await anext(stream)
    assert '"_routing"' in metadata
    assert (await router._pool.snapshot()).reserved_tokens > 0
    await stream.aclose()
    assert not local.calls and not cloud.calls
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_first_visible_reasoning_releases_prefill_only_and_close_releases_rest():
    router, local, cloud, _, _, _ = _build()
    stream = router.stream_chat_completion("model", MESSAGES, max_tokens=100)
    await anext(stream)
    await anext(stream)  # Role-only frame.
    before = await router._pool.snapshot()
    assert len(before.inflight_prefills) == 1
    assert before.inflight_prefills[0].prompt_tokens > 0
    await anext(stream)  # Reasoning is user-visible first output.
    decoding = await router._pool.snapshot()
    assert decoding.inflight_prefills == ()
    assert decoding.reserved_tokens == before.reserved_tokens
    assert decoding.inflight_remaining_s == pytest.approx((0.099,))
    await asyncio.sleep(0.01)
    assert (await router._pool.snapshot()).inflight_remaining_s == decoding.inflight_remaining_s
    await stream.aclose()
    assert local.closed == 1 and not cloud.calls
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_failure_after_visible_content_never_splices_cloud_answer_or_retries():
    router, local, cloud, _, _, _ = _build(local=_Adapter("local", fail_after_content=True))
    received = []
    with pytest.raises(RuntimeError, match="after visible") as exc:
        async for chunk in router.stream_chat_completion("model", MESSAGES, max_tokens=100):
            received.append(chunk)
    assert any("reasoning_content" in chunk for chunk in received)
    assert len(local.calls) == 1 and not cloud.calls
    assert local.closed == 1
    assert exc.value._routing["endpoint_id"] == "local"
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_cancelled_dispatched_local_work_retains_configured_grace():
    router, local, _, _, _, _ = _build(
        config=_config(cancel_grace_s=0.03), local=_Adapter("local", block=True)
    )
    task = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: bool(local.calls))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert local.closed == 1
    assert (await router._pool.snapshot()).reserved_tokens > 0
    await asyncio.sleep(0.04)
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_two_router_instances_cannot_double_count_one_pool_capacity():
    pools = BaselinePoolRegistry()
    first, local1, cloud1, _, _, _ = _build(pools=pools, local=_Adapter("local", block=True))
    second, local2, cloud2, _, _, _ = _build(pools=pools)
    assert first._pool is second._pool
    first_task = asyncio.create_task(first.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: bool(local1.calls))
    response = await second.chat_completion("alias", MESSAGES, max_tokens=100)
    assert response["_routing"]["endpoint_id"] == "api"
    assert not local2.calls and not cloud1.calls and len(cloud2.calls) == 1
    local1.release.set()
    await first_task
    assert await first._pool.snapshot() == BaselineSnapshot()


def test_pool_registry_rejects_capacity_aliases_and_conflicting_profiles():
    pools = BaselinePoolRegistry()
    profile = BaselineProfile(1000, 100, 1000, 0.01, 0, 10, 0)
    pools.get_pool("one", "local", profile, 1)
    with pytest.raises(ValueError, match="different pool"):
        pools.get_pool("two", "local", profile, 1)
    with pytest.raises(ValueError, match="conflict"):
        pools.get_pool("one", "other-local", profile, 1)


async def test_sink_failure_happens_before_any_execution_or_committed_reservation():
    def fail(record):
        raise OSError("decision log unavailable")

    router, local, cloud, _, _, _ = _build(sink=fail)
    with pytest.raises(OSError, match="log unavailable"):
        await router.chat_completion("model", MESSAGES, max_tokens=100)
    assert not local.calls and not cloud.calls
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_cloud_scope_and_local_rejection_do_not_escape_caller_scope():
    router, local, cloud, _, _, _ = _build(config=_config("all_api"))
    response = await router.chat_completion(
        "model", MESSAGES, max_tokens=100, routing_options=RoutingRequestOptions(pin_provider="api")
    )
    assert response["_routing"]["endpoint_id"] == "api"
    with pytest.raises(TargetUnavailableError, match="caller scope"):
        await router.chat_completion(
            "model",
            MESSAGES,
            max_tokens=100,
            routing_options=RoutingRequestOptions(pin_provider="local"),
        )
    assert not local.calls and len(cloud.calls) == 1
    with pytest.raises(TargetUnavailableError, match="no configured endpoint"):
        await router.chat_completion(
            "model",
            MESSAGES,
            max_tokens=100,
            routing_options=RoutingRequestOptions(endpoint_scope=frozenset()),
        )


async def test_request_cannot_replace_metered_cloud_adapter_by_injected_binding():
    router, _, cloud, _, _, _ = _build(config=_config("all_api"))
    unmetered = _Adapter("api")
    with pytest.raises(DispatchMismatchError, match="metered"):
        await router.chat_completion(
            "model",
            MESSAGES,
            max_tokens=100,
            routing_options=RoutingRequestOptions(
                preferred_endpoint_id="api",
                require_target=True,
                bound_endpoint=binding_for_adapter(unmetered, model_id="model"),
            ),
        )
    assert not cloud.calls and not unmetered.calls


async def test_bound_endpoint_requires_matching_model_and_exact_target():
    router, local, cloud, _, _, _ = _build()
    for bound_model, preferred, required in (
        ("other-model", "api", True),
        ("model", "local", True),
        ("model", None, False),
    ):
        with pytest.raises(DispatchMismatchError, match="matching exact"):
            await router.chat_completion(
                "model",
                MESSAGES,
                max_tokens=100,
                routing_options=RoutingRequestOptions(
                    preferred_endpoint_id=preferred,
                    require_target=required,
                    bound_endpoint=binding_for_adapter(cloud, model_id=bound_model),
                ),
            )
    assert not local.calls and not cloud.calls


async def test_tool_and_response_schemas_increase_online_prefill_kv_and_cost_estimates():
    router, _, _, records, _, _ = _build(config=_config("all_api"))
    await router.chat_completion("model", MESSAGES, max_tokens=100)
    await router.chat_completion(
        "model",
        MESSAGES,
        max_tokens=100,
        tools=[{"type": "function", "function": {"name": "tool", "description": "x" * 8000}}],
        response_format={"type": "json_schema", "json_schema": {"description": "y" * 4000}},
    )
    baseline, with_schemas = (record["candidate"] for record in records)
    assert with_schemas["prompt_tokens"] > baseline["prompt_tokens"] + 2900
    assert with_schemas["estimated_remote_cost"] > baseline["estimated_remote_cost"]
    baseline_prediction, schema_prediction = (record["prediction"] for record in records)
    assert (
        schema_prediction["request_commitment_tokens"]
        > baseline_prediction["request_commitment_tokens"] + 2900
    )


async def test_queued_request_preserves_binding_across_route_table_change():
    router, original, cloud, _, _, fixed = _build(config=_config(batch_window_s=0.03))
    task = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: bool(router._pending))
    replacement = _Adapter("local")
    fixed.register_route("model", [(replacement, 1), (cloud, 1)])
    router.refresh_route_table()
    await task
    assert len(original.calls) == 1 and not replacement.calls
    await router.chat_completion("model", MESSAGES, max_tokens=100)
    assert len(replacement.calls) == 1


async def test_stop_rejects_pending_requests_without_dispatch():
    router, local, cloud, _, _, _ = _build(config=_config(batch_window_s=0.1))
    assert await router.start()
    assert not await router.start()
    task = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: bool(router._pending))
    await router.stop()
    with pytest.raises(TargetUnavailableError, match="stopped"):
        await task
    assert not local.calls and not cloud.calls
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_stop_rejects_batch_waiting_for_pool_without_waiting_for_pool_release():
    router, local, cloud, _, _, _ = _build(config=_config(batch_window_s=0))
    await router._pool.lock.acquire()
    try:
        task = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
        await _wait_until(lambda: bool(router._pending))
        await asyncio.sleep(0.01)
        await asyncio.wait_for(router.stop(), 0.2)
        with pytest.raises(TargetUnavailableError, match="stopped"):
            await asyncio.wait_for(task, 0.2)
        assert not local.calls and not cloud.calls
    finally:
        router._pool.lock.release()
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_external_batch_cancellation_completes_pending_futures_without_dispatch():
    router, local, cloud, _, _, _ = _build(config=_config(batch_window_s=0))
    await router._pool.lock.acquire()
    try:
        task = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
        await _wait_until(lambda: bool(router._pending))
        await asyncio.sleep(0.01)
        batch_task = router._batch_task
        batch_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await batch_task
        with pytest.raises(TargetUnavailableError, match="interrupted"):
            await asyncio.wait_for(task, 0.2)
        assert not local.calls and not cloud.calls
    finally:
        router._pool.lock.release()
    assert await router._pool.snapshot() == BaselineSnapshot()


@pytest.mark.parametrize("cap", [None, 0, 101, True])
async def test_fixed_output_cap_is_required_before_any_selection_or_wire(cap):
    router, local, cloud, records, _, _ = _build()
    with pytest.raises(ValueError, match="positive output cap"):
        await router.chat_completion("model", MESSAGES, max_tokens=cap)
    assert not local.calls and not cloud.calls and not records


async def test_conflicting_output_cap_alias_cannot_understate_peak_reservation():
    router, local, cloud, records, _, _ = _build()
    with pytest.raises(ValueError, match="positive output cap"):
        await router.chat_completion("model", MESSAGES, max_tokens=100, max_completion_tokens=1)
    assert not local.calls and not cloud.calls and not records


@pytest.mark.parametrize("policy", ["all_local", "greedy", "rolling_knapsack_v1"])
async def test_local_fifo_waiting_is_distinct_from_one_real_inflight_slot(policy):
    cfg = _config(policy)
    cfg["profile"]["kv_capacity_tokens"] = 10000
    router, local, cloud, records, _, _ = _build(config=cfg, local=_Adapter("local", block=True))

    async def call(identifier):
        with req_ctx.push(request_id=identifier):
            return await router.chat_completion("model", MESSAGES, max_tokens=100)

    tasks = [asyncio.create_task(call(identifier)) for identifier in ("one", "two", "three")]
    await _wait_until(lambda: len(records) == 3 and len(local.calls) == 1)
    snapshot = await router._pool.snapshot()
    assert len(snapshot.waiting) == 2
    assert len(snapshot.inflight_prefills) == 1
    assert snapshot.reserved_tokens > 3 * 100
    assert not cloud.calls
    local.release.set()
    await asyncio.gather(*tasks)
    assert [entry[0]["request_id"] for entry in local.calls] == ["one", "two", "three"]
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_cancellation_while_waiting_for_local_slot_never_dispatches_and_frees_peak_kv():
    cfg = _config("all_local", cancel_grace_s=10)
    router, local, cloud, records, _, _ = _build(config=cfg, local=_Adapter("local", block=True))
    first = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: len(local.calls) == 1)
    second = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: len(records) == 2)
    before = await router._pool.snapshot()
    assert len(before.waiting) == 1
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    after = await router._pool.snapshot()
    assert not after.waiting
    assert after.reserved_tokens < before.reserved_tokens
    assert len(local.calls) == 1 and not cloud.calls
    local.release.set()
    await first
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_cancel_grace_holds_real_slot_and_waiter_wakes_at_expiry_without_new_arrivals():
    router, local, cloud, records, _, _ = _build(
        config=_config("all_local", cancel_grace_s=0.05), local=_Adapter("local", block=True)
    )
    first = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: len(local.calls) == 1)
    second = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: len(records) == 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    snapshot = await router._pool.snapshot()
    assert len(snapshot.waiting) == len(snapshot.inflight_prefills) == 1
    assert snapshot.inflight_prefills[0].slot_release_floor_s >= 0.04
    await asyncio.sleep(0.01)
    assert len(local.calls) == 1
    await _wait_until(lambda: len(local.calls) == 2)
    local.release.set()
    await second
    assert not cloud.calls
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_knapsack_deferred_candidates_roll_immediately_under_same_fifo_snapshot():
    cfg = _config("rolling_knapsack_v1", max_candidates=2, batch_window_s=0.02)
    cfg["profile"]["kv_capacity_tokens"] = 10000
    router, local, cloud, records, _, _ = _build(config=cfg)
    await asyncio.gather(
        *(router.chat_completion("model", MESSAGES, max_tokens=100) for _ in range(5))
    )
    assert len(local.calls) == 5 and not cloud.calls
    assert len({record["decision_start_s"] for record in records}) == 1
    assert all(record["window_truncated"] for record in records)
    windows = records[0]["selection_windows"]
    assert [len(window["candidate_ids"]) for window in windows] == [2, 2, 1]
    assert [len(window["deferred_ids"]) for window in windows] == [3, 1, 0]
    assert not any(record["reason"] == "deferred_window" for record in records)
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_stop_rejects_accepted_waiting_but_keeps_active_and_other_router_owners():
    pools = BaselinePoolRegistry()
    cfg = _config("all_local")
    first, local, _, records, _, _ = _build(
        config=cfg, pools=pools, local=_Adapter("local", block=True)
    )
    other, other_local, _, other_records, _, _ = _build(config=cfg, pools=pools)
    active = asyncio.create_task(first.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: len(local.calls) == 1)
    waiting = asyncio.create_task(first.chat_completion("model", MESSAGES, max_tokens=100))
    other_waiting = asyncio.create_task(other.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: len(records) == 2 and len(other_records) == 1)
    assert len((await first._pool.snapshot()).waiting) == 2
    await first.stop()
    with pytest.raises(TargetUnavailableError, match="stopped"):
        await waiting
    snapshot = await first._pool.snapshot()
    assert len(snapshot.waiting) == len(snapshot.inflight_prefills) == 1
    assert len(local.calls) == 1 and not other_local.calls
    local.release.set()
    await asyncio.gather(active, other_waiting)
    assert len(local.calls) == len(other_local.calls) == 1
    assert await first._pool.snapshot() == BaselineSnapshot()


async def test_max_pending_includes_accepted_waiters_not_just_collection_window():
    cfg = _config("all_local", max_pending=1)
    router, local, cloud, records, _, _ = _build(config=cfg, local=_Adapter("local", block=True))
    active = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: len(local.calls) == 1)
    waiting = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: len(records) == 2)
    with pytest.raises(TargetUnavailableError, match="queue is full"):
        await router.chat_completion("model", MESSAGES, max_tokens=100)
    assert len(local.calls) == 1 and not cloud.calls and len(records) == 2
    local.release.set()
    await asyncio.gather(active, waiting)
    assert await router._pool.snapshot() == BaselineSnapshot()


async def test_cancelled_prefill_stays_in_shared_lane_even_when_another_slot_is_free():
    cfg = _config(cancel_grace_s=1, local_max_inflight=2)
    cfg["profile"].update(kv_capacity_tokens=10000, prefill_tput=10, slo_s=30)
    router, local, cloud, records, _, _ = _build(config=cfg, local=_Adapter("local", block=True))
    first = asyncio.create_task(router.chat_completion("model", MESSAGES, max_tokens=100))
    await _wait_until(lambda: len(local.calls) == 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    local.release.set()
    await router.chat_completion("model", MESSAGES, max_tokens=100)
    state = records[1]["snapshot_before"]
    assert len(state["inflight_prefills"]) == 1 and not state["inflight_remaining_s"]
    assert state["inflight_prefills"][0]["slot_release_floor_s"] > 0
    assert (
        state["inflight_prefills"][0]["prompt_tokens"] == records[0]["candidate"]["prompt_tokens"]
    )
    assert records[1]["prediction"]["predicted_ttft_s"] >= (
        records[0]["candidate"]["prompt_tokens"] / cfg["profile"]["prefill_tput"]
    )
    assert len(local.calls) == 2 and not cloud.calls
