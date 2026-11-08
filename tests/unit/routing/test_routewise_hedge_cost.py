"""RouteWise's per-checkpoint and per-solve work is bounded and gives the same answers.

The hedge planner runs on the event loop under the router's commit lock, at up
to 27 checkpoints of every slow request. Each checkpoint used to scan every
latency profile in full -- the primary's twice per backup candidate -- and then
re-run that for every later checkpoint before it decided to defer: about 2 ms
per checkpoint with full 5000-sample profiles, 90 ms of loop per slow request.
Every solve also tokenized the whole prompt, a quarter of a second at 1 MiB.
"""

from __future__ import annotations

import random
import time
from typing import Any

import pytest

import routing.routewise.router as router_module
from routing.routewise.config import RouteWiseConfig
from routing.routewise.latency import ProviderProfile
from routing.routewise.router import RouteWiseRouter
from tests.unit.routing.test_routewise_router import _FakeRouteTable, _make_adapter

ENDPOINTS = ("m:conc", "m:api-a", "m:api-b")


def _scan_cdf(profile: ProviderProfile, threshold_sec: float, now: float) -> float:
    """The scan the profile used to run, as a reference."""
    profile._prune(now)
    events = list(profile._events)
    timed = [ttft for _ts, ttft, err, _p in events if err is None and ttft > 0]
    if not timed:
        return 0.0
    errors = sum(1 for _ts, _ttft, err, _p in events if err is not None)
    within = sum(1 for ttft in timed if ttft / 1000.0 <= threshold_sec)
    return (1.0 - errors / len(events)) * (within / len(timed))


@pytest.mark.unit
def test_the_bisected_cdf_matches_a_scan_over_a_random_history():
    rng = random.Random(7)
    profile = ProviderProfile(endpoint_id="ep", window_sec=40.0, max_samples=64)
    for step in range(3000):
        now = step * 0.1
        roll = rng.random()
        if roll < 0.15:
            profile.record(now, -1.0, "error")
        elif roll < 0.25:
            profile.record(now, -1.0)
        else:
            # Repeated values on purpose: duplicates must leave one at a time.
            profile.record(now, float(rng.choice((50, 120, 120, 333.3, 900, 2500))))
        if step % 37 == 0:
            for threshold in (0.0, 0.05, 0.12, 0.1203, 0.3333, 0.9, 2.5, 10.0):
                assert profile.cdf_at(threshold, now) == _scan_cdf(profile, threshold, now)
    # Everything ages out: the sorted structure empties with the window.
    assert profile.cdf_at(1.0, 10_000.0) == 0.0
    assert profile._sorted_ttft_sec == []


def _router(seed: int) -> RouteWiseRouter:
    conc = _make_adapter(
        provider_type="concurrency", endpoint_id=ENDPOINTS[0], concurrency={"limit": 4}
    )
    a = _make_adapter(endpoint_id=ENDPOINTS[1], prompt_price="0.15", completion_price="1.15")
    b = _make_adapter(endpoint_id=ENDPOINTS[2], prompt_price="0.3", completion_price="1.2")
    table = _FakeRouteTable()
    table.add("m", [(conc, 1.0), (a, 1.0), (b, 1.0)])
    router = RouteWiseRouter(
        route_table=table,
        config=RouteWiseConfig(
            random_seed=seed, latency_hedge_mode="probability_target", latency_slo_sec=3.0
        ),
    )
    rng = random.Random(seed)
    now = time.time()
    for endpoint in ENDPOINTS:
        profile = router._latency_profiles[endpoint]
        mu = rng.uniform(0.2, 1.2)
        for i in range(400):
            if rng.random() < rng.uniform(0.0, 0.08):
                profile.record(now - 300 + i * 0.5, -1.0, "error")
            else:
                profile.record(now - 300 + i * 0.5, rng.lognormvariate(mu, 0.6) * 300.0)
    return router


def _full_scan_defers(router: RouteWiseRouter, kwargs: dict[str, Any], checkpoints) -> bool:
    """Whether any later checkpoint could still hedge: the scan the router used to run."""
    for future in checkpoints:
        if future <= kwargs["elapsed_sec"] + 1e-9:
            continue
        if router._select_hedge_candidate_at_elapsed(**{**kwargs, "elapsed_sec": future}):
            return True
    return False


@pytest.mark.unit
@pytest.mark.parametrize("seed", range(12))
def test_the_one_checkpoint_look_ahead_decides_like_the_full_scan(seed):
    router = _router(seed)
    context = {"prompt_tokens": 2000, "params": {}, "messages": []}
    decision = router._select_decision("m", context)
    try:
        selector = decision.adapter.checkpoint_backup_selector
        checkpoints = decision.adapter.hedge_checkpoints_sec
        selected_endpoint = decision.metadata["selected_endpoint"]
        for checkpoint in checkpoints:
            now = time.time()
            candidates, _ = router._build_candidates(
                "m", prompt_tokens=2000, predicted_output_tokens=512.0, envelope=None, now=now
            )
            kwargs = {
                "primary_profile": router._latency_profiles[selected_endpoint],
                "candidates": candidates,
                "selected": next(c for c in candidates if c.endpoint_id == selected_endpoint),
                "now": now,
                "elapsed_sec": checkpoint,
                "latency_slo_sec": 3.0,
            }
            current = router._select_hedge_candidate_at_elapsed(**kwargs)
            expected = current is not None and not _full_scan_defers(router, kwargs, checkpoints)

            dispatch = selector(checkpoint, now)
            assert (dispatch is not None) == expected, checkpoint
            if dispatch is not None:
                assert dispatch.backup.config.endpoint_id == current.provider.endpoint_id
                dispatch.release()
                break
    finally:
        decision.release()


@pytest.mark.unit
def test_a_checkpoint_reads_the_primary_cdf_once_per_value(monkeypatch):
    router = _router(3)
    decision = router._select_decision("m", {"prompt_tokens": 2000, "params": {}, "messages": []})
    try:
        primary = router._latency_profiles[decision.metadata["selected_endpoint"]]
        calls: list[float] = []
        original = primary.cdf_at

        def counting(threshold_sec: float, current_time: float) -> float:
            calls.append(threshold_sec)
            return original(threshold_sec, current_time)

        monkeypatch.setattr(primary, "cdf_at", counting)
        checkpoints = decision.adapter.hedge_checkpoints_sec
        dispatch = decision.adapter.checkpoint_backup_selector(checkpoints[0], time.time())
        if dispatch is not None:
            dispatch.release()
    finally:
        decision.release()

    # This checkpoint's elapsed time, the SLO, and the next checkpoint: once
    # each, however many backup candidates there are.
    assert len(calls) == len(set(calls)) <= 3


@pytest.mark.unit
def test_a_request_is_estimated_once_and_boundedly(monkeypatch):
    calls: list[Any] = []
    bounded = router_module.estimate_prompt_tokens_bounded

    def counting(messages, **kwargs):
        calls.append(messages)
        return bounded(messages, **kwargs)

    monkeypatch.setattr(router_module, "estimate_prompt_tokens_bounded", counting)
    assert not hasattr(router_module, "estimate_prompt_tokens")
    router = _router(1)
    messages = [{"role": "user", "content": "word " * 200_000}]
    context = {"messages": messages, "params": {}}

    router._select_decision("m", context).release()
    router._select_decision("m", context).release()

    assert calls == [messages]
    assert context["prompt_tokens_estimate"] == (messages, bounded(messages))
