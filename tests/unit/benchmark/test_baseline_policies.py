"""Compare baseline decisions against frozen source excerpts, not a second model.

The reference below is verbatim AST source extraction from the dirty working copy
identified by SOURCE_PROVENANCE. It has no external repository imports and never
opens an endpoint. Tests exercise live-input states, not future trace outcomes.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import math
import random
from dataclasses import asdict, fields, replace
from types import SimpleNamespace

import pytest

from benchmark.nimbus.baselines import (
    SOURCE_PROVENANCE,
    BaselineProfile,
    BaselineRequest,
    BaselineSnapshot,
    InflightPrefill,
    predict_waiting_ttfts,
    request_commitment_tokens,
    select_baseline,
)

# Exact excerpts: common.py effective_decode/local_prompt_tokens;
# nimbus.py predicted_waiting_ttfts_s; greedy.py full_prompt_request/GreedyPolicy.
_LEGACY_SHA256 = "55b4dc1d7295ecada48ebe1734e652c2d1a0161368becb20b1632c10d7a9de8a"
_LEGACY_SOURCE = r'''def effective_decode(
    req: dict[str, Any],
    max_tokens_override: int | None = None,
) -> int:
    """Decode length requested by both real and fake endpoints."""
    return (
        int(max_tokens_override)
        if max_tokens_override is not None
        else int(req["max_tokens"])
    )

def local_prompt_tokens(req: dict[str, Any]) -> int:
    """Marginal prompt KV added locally, falling back to the full prompt."""
    uncached = req.get("uncached_prompt_tokens")
    if uncached is not None:
        return int(uncached)
    return int(req.get("prompt_tokens") or 0)

def predicted_waiting_ttfts_s(
    waiting: list[dict[str, Any]],
    context: DecisionContext,
    *,
    prefill_tput: float,
    tpot_s: float,
    first_token_overhead_s: float | None = None,
    max_tokens_override: int | None = None,
) -> list[float]:
    """Predict from-arrival TTFT over sequence slots plus shared prefill.

    This is a calibrated online approximation, not an engine-exact simulator.
    Decode length is the trace/request cap in this first experiment (an oracle
    input whose estimator ablation is intentionally separate).  Prefill work
    is serialized because continuous-batching engines share a bounded prefill
    token budget: giving 128 requests 128 free sequence slots does not make all
    128 first tokens simultaneous.
    """
    if context.max_inflight <= 0:
        raise ValueError("max_inflight must be positive")
    first_token_s = (
        tpot_s
        if first_token_overhead_s is None
        else float(first_token_overhead_s)
    )
    if first_token_s < 0:
        raise ValueError("first_token_overhead_s must be non-negative")
    if any(
        state.prompt_tokens < 0 or state.decode_tokens < 0
        for state in context.inflight_prefills
    ):
        raise ValueError("inflight prefill token counts must be non-negative")

    slots = [max(0.0, float(x)) for x in context.inflight_remaining_s]
    prefill_ready_s = 0.0
    for state in context.inflight_prefills:
        prefill_ready_s += (
            float(state.prompt_tokens) / max(prefill_tput, 1e-9)
        )
        heapq.heappush(
            slots,
            prefill_ready_s
            + first_token_s
            + max(0, state.decode_tokens - 1) * tpot_s,
        )
    # Runtime invariants keep len(inflight) <= max_inflight.  If an injected
    # context violates that, retain every known busy slot rather than silently
    # discarding work and becoming optimistic.
    slot_count = max(context.max_inflight, len(slots))
    slots.extend([0.0] * (slot_count - len(slots)))
    heapq.heapify(slots)

    # The tracker cannot observe partial prefill progress, so each admitted
    # prefill above retains its full work until exact output usage proves it
    # reached decode.  This is conservative without inventing engine progress.
    predictions: list[float] = []
    for req in waiting:
        slot_ready_s = heapq.heappop(slots)
        prompt = local_prompt_tokens(req)
        decode = effective_decode(req, max_tokens_override)
        prefill_s = prompt / max(prefill_tput, 1e-9)
        prefill_started_s = max(slot_ready_s, prefill_ready_s)
        prefill_done_s = prefill_started_s + prefill_s
        age_s = max(0.0, float(context.waiting_age_s.get(req.get("request_id"), 0.0)))
        # TTFT includes the fitted fixed first-token overhead. Slot residence
        # includes all requested decode steps after prefill before the next
        # queued request can be admitted; waiting prefill cannot overtake it.
        predictions.append(age_s + prefill_done_s + first_token_s)
        heapq.heappush(
            slots,
            prefill_done_s
            + first_token_s
            + max(0, decode - 1) * tpot_s,
        )
        prefill_ready_s = prefill_done_s
    return predictions

def full_prompt_request(req: dict[str, Any]) -> dict[str, Any]:
    """Use full prompt work for this no-cache baseline."""
    return {**req, "uncached_prompt_tokens": req["prompt_tokens"]}

class GreedyPolicy:
    name = "greedy"
    p = None

    def __init__(
        self, *, kv_capacity_tokens: float, max_tokens: int,
        prefill_tput: float, tpot_s: float, first_token_overhead_s: float,
        slo_s: float, ttft_guard_s: float, kv_block_size: int = 16,
    ):
        values = (kv_capacity_tokens, prefill_tput, tpot_s,
                  first_token_overhead_s, slo_s, ttft_guard_s)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("greedy calibration and budgets must be finite")
        if kv_capacity_tokens <= 0 or prefill_tput <= 0 or slo_s <= 0:
            raise ValueError("greedy capacity, prefill throughput and SLO must be positive")
        if tpot_s < 0 or first_token_overhead_s < 0:
            raise ValueError("greedy service times must be non-negative")
        if not 0 <= ttft_guard_s < slo_s:
            raise ValueError("greedy TTFT guard must be in [0, SLO)")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("greedy requires a positive explicit generation cap")
        if isinstance(kv_block_size, bool) or not isinstance(kv_block_size, int) or kv_block_size <= 0:
            raise ValueError("greedy KV block size must be a positive integer")
        self.kv_capacity_tokens = kv_capacity_tokens
        self.max_tokens = max_tokens
        self.kv_block_size = kv_block_size
        self.prefill_tput = prefill_tput
        self.tpot_s = tpot_s
        self.first_token_overhead_s = first_token_overhead_s
        self.ttft_budget_s = slo_s - ttft_guard_s
        self._reservations: dict[Any, int] = {}
        self.reserved_tokens = 0
        self.peak_reserved_tokens = 0
        self.n_total = 0
        self.n_outsourced = 0
        self.reasons = {"local_feasible": 0, "kv_capacity": 0, "ttft_budget": 0}
        self.last_decision: dict[str, Any] = {}

    @property
    def actual_fraction(self) -> float:
        return self.n_outsourced / self.n_total if self.n_total else 0.0

    def validate_trace(self, trace: list[dict[str, Any]]) -> None:
        """Check the input contract before opening any endpoint connection."""
        seen = set()
        for req in trace:
            rid = req.get("request_id")
            if rid is None or rid in seen:
                raise ValueError("greedy requires unique, non-null request ids")
            seen.add(rid)
            prompt = req.get("prompt_tokens")
            if isinstance(prompt, bool) or not isinstance(prompt, int) or prompt < 0:
                raise ValueError("greedy requires tokenizer-aligned integer prompt_tokens >= 0")

    def outsource(
        self, req: dict[str, Any], *, waiting: list[dict[str, Any]],
        context: DecisionContext,
    ) -> bool:
        peak_tokens = req["prompt_tokens"] + self.max_tokens
        commitment = ((peak_tokens + self.kv_block_size - 1)
                      // self.kv_block_size * self.kv_block_size)
        before = self.reserved_tokens
        predicted_ttft_s = None
        if before + commitment > self.kv_capacity_tokens:
            reason = "kv_capacity"
        else:
            # Only already-accepted FIFO work and this arrival are visible.
            # The explicit cap replaces all trace decode lengths in prediction.
            predicted_ttft_s = predicted_waiting_ttfts_s(
                [full_prompt_request(r) for r in [*waiting, req]], context,
                prefill_tput=self.prefill_tput, tpot_s=self.tpot_s,
                first_token_overhead_s=self.first_token_overhead_s,
                max_tokens_override=self.max_tokens,
            )[-1]
            reason = ("local_feasible" if predicted_ttft_s <= self.ttft_budget_s
                      else "ttft_budget")
        cloud = reason != "local_feasible"
        self.n_total += 1
        self.n_outsourced += int(cloud)
        self.reasons[reason] += 1
        if not cloud:
            self._reservations[req["request_id"]] = commitment
            self.reserved_tokens += commitment
            self.peak_reserved_tokens = max(self.peak_reserved_tokens, self.reserved_tokens)
        self.last_decision = {
            "policy": self.name, "event": "arrival", "request_id": req["request_id"],
            "route": "cloud" if cloud else "local", "reason": reason,
            "predicted_ttft_s": predicted_ttft_s, "ttft_budget_s": self.ttft_budget_s,
            "reserved_tokens_before": before, "reserved_tokens_after": self.reserved_tokens,
            "request_commitment_tokens": commitment,
            "decode_information": "explicit_generation_cap",
        }
        return cloud

    def release(self, request_id: Any) -> None:
        """Keep the peak reservation until completion, failure or cancellation."""
        self.reserved_tokens -= self._reservations.pop(request_id, 0)

    def clear_reservations(self) -> None:
        self._reservations.clear()
        self.reserved_tokens = 0'''


_LEGACY = {
    "math": math,
    "heapq": heapq,
    "Any": object,
    "DecisionContext": object,
}
exec(
    compile("from __future__ import annotations\n" + _LEGACY_SOURCE, "frozen_prototype", "exec"),
    _LEGACY,
)


def _profile(**overrides):
    return BaselineProfile(
        **{
            "kv_capacity_tokens": 10_000,
            "max_tokens": 16,
            "prefill_tput": 1000,
            "tpot_s": 0.005,
            "first_token_overhead_s": 0.01,
            "slo_s": 1,
            "ttft_guard_s": 0.05,
            "kv_block_size": 1,
            **overrides,
        }
    )


def _request(request_id, prompt=100, *, arrival=0.0, cost=1.0):
    return BaselineRequest(str(request_id), arrival, prompt, cost)


def _row(request):
    return {
        "request_id": request.request_id,
        "prompt_tokens": request.prompt_tokens,
        # These are deliberately invalid future/cache hints. Source greedy
        # replaces both fields with its explicit cap and full prompt.
        "max_tokens": object(),
        "uncached_prompt_tokens": 0,
    }


def _context(requests, snapshot, now_s):
    return SimpleNamespace(
        waiting_age_s={r.request_id: now_s - r.arrival_s for r in requests},
        inflight_remaining_s=snapshot.inflight_remaining_s,
        inflight_prefills=snapshot.inflight_prefills,
        max_inflight=snapshot.max_inflight,
    )


def _legacy_run(candidates, snapshot, profile, now_s):
    legacy = _LEGACY["GreedyPolicy"](**asdict(profile))
    legacy.reserved_tokens = snapshot.reserved_tokens
    waiting = [_row(request) for request in snapshot.waiting]
    context = _context([*snapshot.waiting, *candidates], snapshot, now_s)
    decisions = []
    for request in sorted(candidates, key=lambda r: r.arrival_s):
        if request.arrival_s > now_s:
            continue
        row = _row(request)
        cloud = legacy.outsource(row, waiting=waiting, context=context)
        decisions.append(dict(legacy.last_decision))
        if not cloud:
            waiting.append(row)
    return decisions


def test_frozen_reference_provenance():
    """Pin the exact extracted source text and its working-copy provenance."""
    assert hashlib.sha256(_LEGACY_SOURCE.encode()).hexdigest() == _LEGACY_SHA256
    assert SOURCE_PROVENANCE["files"]["router/greedy.py"]["working_copy_status"] == "untracked"
    assert SOURCE_PROVENANCE["head"] == "13694272a41a10c9cf2eb6984a16b10115016adc"


def test_greedy_matches_actual_source_across_generated_runtime_states():
    """Match source reasons, predictions and reservations under mixed runtime states."""
    rng = random.Random(710041)
    reasons = set()
    for case in range(120):
        profile = _profile(
            kv_capacity_tokens=rng.choice([240, 1000, 10_000]),
            max_tokens=rng.choice([1, 16, 64]),
            kv_block_size=rng.choice([1, 16, 32]),
            prefill_tput=rng.choice([300, 1000, 5000]),
            tpot_s=rng.choice([0, 0.005, 0.05]),
            slo_s=rng.choice([0.2, 1, 3]),
            ttft_guard_s=rng.choice([0, 0.01, 0.05]),
        )
        waiting = tuple(
            _request(f"w{i}", rng.randrange(0, 300), arrival=-rng.random())
            for i in range(rng.randrange(4))
        )
        snapshot = BaselineSnapshot(
            reserved_tokens=sum(request_commitment_tokens(r, profile) for r in waiting)
            + rng.choice([0, 256, 1024]),
            waiting=waiting,
            inflight_remaining_s=tuple(rng.uniform(-0.1, 1) for _ in range(rng.randrange(4))),
            max_inflight=rng.choice([1, 2, 4]),
            inflight_prefills=tuple(
                InflightPrefill(rng.randrange(0, 2000), profile.max_tokens)
                for _ in range(rng.randrange(3))
            ),
        )
        candidates = [
            _request(f"{case}-{i}", rng.randrange(0, 2000), arrival=-rng.choice([0, 0.1, 0.8]))
            for i in range(rng.randrange(1, 9))
        ]
        expected = _legacy_run(candidates, snapshot, profile, 0)
        actual = select_baseline("greedy", candidates, snapshot, profile, 0)
        assert len(actual.decisions) == len(expected)
        for decision, source in zip(actual.decisions, expected, strict=True):
            reasons.add(decision.reason)
            assert decision.request_id == source["request_id"]
            assert decision.selected == (source["route"] == "local")
            assert decision.reason == source["reason"]
            assert decision.predicted_ttft_s == source["predicted_ttft_s"]
            assert decision.request_commitment_tokens == source["request_commitment_tokens"]
            assert decision.reserved_tokens_before == source["reserved_tokens_before"]
            assert decision.reserved_tokens_after == source["reserved_tokens_after"]
    assert reasons == {"local_feasible", "kv_capacity", "ttft_budget"}


def test_predictor_matches_source_with_mixed_active_phases_and_fifo_queue():
    """Retain source slot and shared-prefill behavior even for oversubscribed input."""
    profile = _profile(max_tokens=3)
    snapshot = BaselineSnapshot(
        inflight_remaining_s=(0.3, -0.1),
        max_inflight=1,
        inflight_prefills=(InflightPrefill(200, 3), InflightPrefill(500, 3)),
    )
    waiting = [_request("old", 100, arrival=-0.2), _request("new", 100)]
    expected = _LEGACY["predicted_waiting_ttfts_s"](
        [_LEGACY["full_prompt_request"](_row(r)) for r in waiting],
        _context(waiting, snapshot, 0),
        prefill_tput=profile.prefill_tput,
        tpot_s=profile.tpot_s,
        first_token_overhead_s=profile.first_token_overhead_s,
        max_tokens_override=profile.max_tokens,
    )
    assert predict_waiting_ttfts(waiting, snapshot, profile, 0) == tuple(expected)


@pytest.mark.parametrize("policy", ["greedy", "rolling_knapsack_v1"])
def test_canceled_prefill_keeps_shared_work_and_slot_release_floor(policy):
    """Retain both cancellation liabilities even when a second slot is free."""
    profile = _profile(slo_s=0.5)
    request = _request("new")
    snapshot = BaselineSnapshot(
        reserved_tokens=1016,
        max_inflight=2,
        inflight_prefills=(InflightPrefill(1000, 16, slot_release_floor_s=5),),
    )
    assert predict_waiting_ttfts([request], snapshot, profile, 0) == pytest.approx((1.11,))
    assert select_baseline(policy, [request], snapshot, profile, 0).selected_ids == ()
    one_slot = replace(snapshot, max_inflight=1)
    assert predict_waiting_ttfts([request], one_slot, profile, 0) == pytest.approx((5.11,))
    normal = replace(one_slot, inflight_prefills=(InflightPrefill(1000, 16),))
    assert predict_waiting_ttfts([request], normal, profile, 0) == pytest.approx((1.195,))


@pytest.mark.parametrize("floor", [-1, float("inf"), float("nan")])
def test_slot_release_floor_rejects_invalid_durations(floor):
    """Keep the cancellation floor finite and nonnegative."""
    with pytest.raises(ValueError, match="slot_release_floor_s"):
        InflightPrefill(1000, 16, slot_release_floor_s=floor)


def test_block_rounding_and_source_slot_wait_example():
    """Preserve source page rounding and admission while waiting for a sequence slot."""
    profile = _profile(kv_capacity_tokens=240, kv_block_size=16)
    result = select_baseline(
        "greedy", [_request("a"), _request("b")], BaselineSnapshot(), profile, 0
    )
    assert result.selected_ids == ("a",)
    assert result.resulting_snapshot.reserved_tokens == 128
    assert result.decisions[1].reason == "kv_capacity"
    assert result.decisions[1].predicted_ttft_s is None
    occupied = BaselineSnapshot(reserved_tokens=128, inflight_remaining_s=(0.1,), max_inflight=1)
    result = select_baseline("greedy", [_request("queued")], occupied, _profile(), 0)
    assert result.selected_ids == ("queued",)
    assert result.decisions[0].predicted_ttft_s == pytest.approx(0.21)
    assert (
        select_baseline("concurrency", [_request("queued")], occupied, _profile(), 0).selected_ids
        == ()
    )


def test_bad_head_does_not_block_later_short_request():
    """Continue FIFO scanning after an infeasible large arrival."""
    result = select_baseline(
        "greedy", [_request("too_long", 2000), _request("short")], BaselineSnapshot(), _profile(), 0
    )
    assert result.selected_ids == ("short",)
    assert [d.reason for d in result.decisions] == ["ttft_budget", "local_feasible"]


@pytest.mark.parametrize("policy", ["greedy", "rolling_knapsack_v1"])
def test_same_time_fifo_ties_and_no_future_arrival_or_output_fields(policy):
    """Keep source tie order and exclude unavailable arrival or output information."""
    candidates = [
        _request("z", cost=2),
        _request("a", cost=2),
        _request("future", arrival=1, cost=1e9),
    ]
    result = select_baseline(
        policy, candidates, BaselineSnapshot(), _profile(kv_capacity_tokens=116), 0
    )
    assert result.selected_ids == ("z",)
    assert result.decisions[-1].reason == "not_arrived"
    assert result.deferred_ids == ()
    assert {field.name for field in fields(BaselineRequest)} == {
        "request_id",
        "arrival_s",
        "prompt_tokens",
        "estimated_remote_cost",
    }


@pytest.mark.parametrize("policy", ["greedy", "rolling_knapsack_v1"])
def test_existing_waiting_is_immutable_and_counts_toward_kv_and_ttft(policy):
    """Keep previous commitments while selecting only new unsent requests."""
    profile = _profile(kv_capacity_tokens=232)
    prior = _request("accepted")
    snapshot = BaselineSnapshot(reserved_tokens=116, waiting=(prior,))
    result = select_baseline(policy, [_request("a"), _request("b")], snapshot, profile, 0)
    assert result.selected_ids == ("a",)
    assert result.resulting_snapshot.waiting == (prior, _request("a"))
    assert snapshot.waiting == (prior,)
    assert snapshot.reserved_tokens == 116
    delayed = BaselineSnapshot(
        reserved_tokens=1016,
        waiting=(_request("long_accepted", 1000),),
    )
    assert select_baseline(policy, [_request("a")], delayed, _profile(), 0).selected_ids == ()


def test_knapsack_finds_better_value_using_the_same_feasible_fifo_subsets():
    """Select the best packing without changing feasibility or dispatch order."""
    profile = _profile(kv_capacity_tokens=20, max_tokens=2)
    candidates = [_request("a", 10, cost=8), _request("b", 8, cost=7), _request("c", 8, cost=7)]
    greedy = select_baseline("greedy", candidates, BaselineSnapshot(), profile, 0)
    knapsack = select_baseline("rolling_knapsack_v1", candidates, BaselineSnapshot(), profile, 0)
    assert greedy.selected_ids == ("a",)
    assert knapsack.selected_ids == ("b", "c")
    assert knapsack.saved_cost == 14
    assert knapsack.resulting_snapshot.reserved_tokens == 20
    assert knapsack.decisions[0].reason == "not_selected"
    assert not knapsack.window_truncated


def test_knapsack_exactness_against_source_feasible_subsets_in_small_windows():
    """Compare bounded selection with exhaustive source-baseline feasibility."""
    rng = random.Random(104102)
    for _ in range(35):
        profile = _profile(
            kv_capacity_tokens=rng.choice([64, 240, 1024]),
            max_tokens=rng.choice([1, 16]),
            kv_block_size=16,
            slo_s=rng.choice([0.2, 1]),
        )
        snapshot = BaselineSnapshot(
            reserved_tokens=64,
            max_inflight=rng.choice([1, 2]),
            inflight_remaining_s=(rng.choice([0.01, 0.15]),),
            inflight_prefills=(InflightPrefill(rng.choice([0, 100]), profile.max_tokens),),
        )
        candidates = [
            _request(
                i, rng.randrange(0, 300), arrival=-rng.choice([0, 0.1]), cost=rng.randrange(0, 8)
            )
            for i in range(6)
        ]
        ordered = sorted(candidates, key=lambda r: r.arrival_s)
        feasible = []
        for size in range(len(ordered) + 1):
            for indices in itertools.combinations(range(len(ordered)), size):
                subset = [ordered[i] for i in indices]
                decisions = _legacy_run(subset, snapshot, profile, 0)
                if all(row["route"] == "local" for row in decisions):
                    feasible.append(
                        (-math.fsum(r.estimated_remote_cost for r in subset), -size, indices)
                    )
        best = min(feasible)
        actual = select_baseline("rolling_knapsack_v1", candidates, snapshot, profile, 0)
        assert actual.selected_ids == tuple(ordered[i].request_id for i in best[2])
        assert actual.saved_cost == -best[0]


def test_knapsack_excess_candidates_remain_undecided_and_roll_without_extra_wait():
    """Process every arrived candidate without window-driven forced offloading."""
    candidates = [_request(i, 0) for i in range(7)]
    snapshot = BaselineSnapshot(max_inflight=8)
    profile = _profile()
    first = select_baseline(
        "rolling_knapsack_v1", candidates, snapshot, profile, 0, max_candidates=3
    )
    assert first.window_ids == ("0", "1", "2")
    assert first.window_truncated
    assert first.deferred_ids == ("3", "4", "5", "6")
    assert all(d.reason == "deferred_window" for d in first.decisions[3:])
    second = select_baseline(
        "rolling_knapsack_v1",
        candidates[3:],
        first.resulting_snapshot,
        profile,
        0,
        max_candidates=3,
    )
    third = select_baseline(
        "rolling_knapsack_v1",
        candidates[6:],
        second.resulting_snapshot,
        profile,
        0,
        max_candidates=3,
    )
    assert first.selected_ids + second.selected_ids + third.selected_ids == tuple(
        str(i) for i in range(7)
    )
    assert third.resulting_snapshot.reserved_tokens == 7 * profile.max_tokens
    assert not third.window_truncated


@pytest.mark.parametrize(
    "costs",
    [
        [0.0] * 6,
        [0.1, 0.2, 0.30000000000000004, 0.3, math.nextafter(0.3, math.inf), 0.0],
        [1.0, math.nextafter(1.0, math.inf), math.nextafter(1.0, -math.inf), 1.0, 1e-20, 0.0],
        [1e16, 1.0, 1e16 - 2, 1e-16, 0.0, 1.0],
    ],
)
def test_knapsack_bound_preserves_ties_and_near_equal_float_objectives(costs):
    """Keep the exhaustive optimum despite rounding, zero costs and close bounds."""
    profile = _profile(kv_capacity_tokens=24, max_tokens=2)
    snapshot = BaselineSnapshot(max_inflight=4)
    candidates = [
        _request(i, prompt, cost=cost)
        for i, (prompt, cost) in enumerate(zip([2, 7, 4, 6, 10, 1], costs, strict=True))
    ]
    feasible = []
    for size in range(len(candidates) + 1):
        for indices in itertools.combinations(range(len(candidates)), size):
            subset = [candidates[i] for i in indices]
            if all(row["route"] == "local" for row in _legacy_run(subset, snapshot, profile, 0)):
                feasible.append(
                    (-math.fsum(r.estimated_remote_cost for r in subset), -size, indices)
                )
    best = min(feasible)
    actual = select_baseline("rolling_knapsack_v1", candidates, snapshot, profile, 0)
    assert actual.selected_ids == tuple(candidates[i].request_id for i in best[2])
    assert actual.saved_cost == -best[0]


def test_controls_share_inputs_and_do_not_change_greedy_constraints():
    """Make the control policies distinct from the shared greedy feasibility model."""
    candidates = [_request("a"), _request("b"), _request("future", arrival=1)]
    snapshot = BaselineSnapshot(max_inflight=1)
    profile = _profile(kv_capacity_tokens=1, slo_s=0.1, ttft_guard_s=0)
    assert select_baseline("all_local", candidates, snapshot, profile, 0).selected_ids == ("a", "b")
    assert select_baseline("all_api", candidates, snapshot, profile, 0).selected_ids == ()
    assert select_baseline("concurrency", candidates, snapshot, profile, 0).selected_ids == ("a",)
    assert select_baseline("greedy", candidates, snapshot, profile, 0).selected_ids == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"kv_capacity_tokens": 0},
        {"prefill_tput": float("nan")},
        {"slo_s": float("inf")},
        {"tpot_s": -0.1},
        {"first_token_overhead_s": -0.1},
        {"ttft_guard_s": 1},
        {"max_tokens": True},
        {"max_tokens": 0},
        {"kv_block_size": 0},
    ],
)
def test_profile_rejects_invalid_calibration(overrides):
    """Refuse nonfinite or invalid model inputs before selection."""
    with pytest.raises(ValueError):
        _profile(**overrides)


def test_invalid_snapshot_overlap_and_window_fail_before_selection():
    """Reject double counting, missing reservations and unbounded search windows."""
    request = _request("a")
    profile = _profile()
    snapshot = BaselineSnapshot(reserved_tokens=116, waiting=(request,))
    with pytest.raises(ValueError, match="unique"):
        select_baseline("greedy", [request], snapshot, profile, 0)
    with pytest.raises(ValueError, match="reserved_tokens"):
        select_baseline("greedy", [], replace(snapshot, reserved_tokens=0), profile, 0)
    with pytest.raises(ValueError, match="already have arrived"):
        select_baseline(
            "greedy", [], replace(snapshot, waiting=(_request("future", arrival=1),)), profile, 0
        )
    for window in (0, 17, True):
        with pytest.raises(ValueError, match="max_candidates"):
            select_baseline(
                "rolling_knapsack_v1",
                [request],
                BaselineSnapshot(),
                profile,
                0,
                max_candidates=window,
            )
