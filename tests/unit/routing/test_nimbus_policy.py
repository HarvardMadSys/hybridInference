"""Admission decisions use online estimates and one shared resource snapshot."""

from __future__ import annotations

from dataclasses import replace

import pytest

from routing.nimbus_policy import Candidate, CapacitySnapshot, LocalProfile, select_requests


def _profile(**changes) -> LocalProfile:
    return replace(
        LocalProfile(
            prefill_tokens_per_s=100,
            decode_tokens_per_s=10,
            max_running_requests=4,
            max_reserved_tokens=10_000,
            max_work_s=100,
        ),
        **changes,
    )


def _candidate(request_id: str, **changes) -> Candidate:
    return replace(
        Candidate(
            request_id=request_id,
            arrival_s=0,
            deadline_s=100,
            prompt_tokens=100,
            uncached_prefill_tokens=100,
            estimated_output_tokens=10,
            estimated_remote_cost=1,
        ),
        **changes,
    )


def _decisions(selection):
    return {decision.request_id: decision for decision in selection.decisions}


@pytest.mark.parametrize("policy", ["fifo", "greedy", "density", "knapsack"])
def test_all_policies_reserve_against_existing_local_work(policy):
    snapshot = CapacitySnapshot(1, 300, 50, 20)
    result = select_requests(
        policy,
        [_candidate("a"), _candidate("b")],
        snapshot,
        _profile(max_running_requests=2),
        now_s=0,
    )
    assert result.selected_ids == ("a",)
    assert result.resulting_snapshot == CapacitySnapshot(2, 410, 150, 30)
    assert snapshot == CapacitySnapshot(1, 300, 50, 20)
    assert _decisions(result)["b"].reason == "concurrency_capacity"


def test_fifo_is_deterministic_in_arrival_then_id_order():
    candidates = [_candidate("c", arrival_s=1), _candidate("b"), _candidate("a")]
    result = select_requests("fifo", candidates, CapacitySnapshot(), _profile(), now_s=1)
    assert result.selected_ids == ("a", "b", "c")
    assert candidates[0].request_id == "c"
    assert result == select_requests(
        "fifo", list(reversed(candidates)), CapacitySnapshot(), _profile(), now_s=1
    )


def test_density_prefers_api_savings_per_work_second_under_contention():
    candidates = [
        _candidate("a", estimated_remote_cost=1),
        _candidate("b", estimated_remote_cost=3),
    ]
    profile = _profile(max_work_s=2)
    fifo = select_requests("fifo", candidates, CapacitySnapshot(), profile, now_s=0)
    density = select_requests("density", candidates, CapacitySnapshot(), profile, now_s=0)
    assert fifo.selected_ids == ("a",)
    assert density.selected_ids == ("b",)


def test_knapsack_combination_can_beat_value_density_greedy():
    candidates = [
        _candidate(
            name,
            prompt_tokens=weight,
            uncached_prefill_tokens=weight,
            estimated_output_tokens=0,
            estimated_remote_cost=value,
        )
        for name, weight, value in [("a", 10, 60), ("b", 20, 100), ("c", 30, 120)]
    ]
    profile = _profile(prefill_tokens_per_s=1, max_work_s=50)
    density = select_requests("density", candidates, CapacitySnapshot(), profile, now_s=0)
    knapsack = select_requests("knapsack", candidates, CapacitySnapshot(), profile, now_s=0)
    assert density.selected_ids == ("a", "b")
    assert knapsack.selected_ids == ("b", "c")
    assert sum(d.value for d in knapsack.decisions if d.selected) == 220


@pytest.mark.parametrize("policy", ["fifo", "density", "knapsack"])
def test_cached_prefix_reduces_prefill_but_not_context_reservation(policy):
    candidates = [
        _candidate("cold", prompt_tokens=1000, uncached_prefill_tokens=1000, deadline_s=2),
        _candidate("warm", prompt_tokens=1000, uncached_prefill_tokens=100, deadline_s=2),
    ]
    result = select_requests(policy, candidates, CapacitySnapshot(), _profile(), now_s=0)
    assert result.selected_ids == ("warm",)
    decision = _decisions(result)["warm"]
    assert decision.predicted_ttft_s == 1
    assert decision.reserved_tokens == 1010
    assert result.resulting_snapshot.reserved_tokens == 1010
    assert _decisions(result)["cold"].reason == "ttft_deadline"


@pytest.mark.parametrize("policy", ["fifo", "density", "knapsack"])
@pytest.mark.parametrize(
    ("profile", "snapshot", "reason"),
    [
        (
            _profile(max_running_requests=1),
            CapacitySnapshot(running_requests=1),
            "concurrency_capacity",
        ),
        (
            _profile(max_reserved_tokens=500),
            CapacitySnapshot(reserved_tokens=400),
            "token_capacity",
        ),
        (_profile(max_work_s=2), CapacitySnapshot(remaining_decode_tokens=10), "work_capacity"),
        (
            _profile(max_predicted_tpot_s=0.15),
            CapacitySnapshot(running_requests=1),
            "tpot_capacity",
        ),
    ],
)
def test_independent_capacity_ceiling_reasons(policy, profile, snapshot, reason):
    result = select_requests(policy, [_candidate("a")], snapshot, profile, now_s=0)
    assert not result.selected_ids
    assert result.decisions[0].reason == reason
    assert result.resulting_snapshot == snapshot


def test_ttft_includes_gateway_wait_and_outstanding_work():
    result = select_requests(
        "fifo",
        [_candidate("a", deadline_s=5)],
        CapacitySnapshot(1, 300, 100, 20),
        _profile(ttft_overhead_s=0.2, decode_interference=0.5),
        now_s=2,
    )
    assert not result.selected_ids
    assert result.decisions[0].reason == "ttft_deadline"
    assert result.decisions[0].predicted_ttft_s == pytest.approx(5.2)


def test_selected_tpot_predictions_include_all_selected_streams():
    result = select_requests(
        "fifo",
        [_candidate("a"), _candidate("b"), _candidate("c")],
        CapacitySnapshot(),
        _profile(max_predicted_tpot_s=0.2),
        now_s=0,
    )
    assert result.selected_ids == ("a", "b")
    assert _decisions(result)["a"].predicted_tpot_s == 0.2
    assert _decisions(result)["b"].predicted_tpot_s == 0.2
    assert _decisions(result)["c"].reason == "tpot_capacity"
    assert _decisions(result)["c"].predicted_tpot_s == 0.3


def test_knapsack_accounts_for_prefill_order_and_deadline_of_each_request():
    candidates = [_candidate("a", deadline_s=3), _candidate("b", deadline_s=1.5)]
    result = select_requests("knapsack", candidates, CapacitySnapshot(), _profile(), now_s=0)
    assert result.selected_ids == ("b", "a")
    assert _decisions(result)["b"].predicted_ttft_s == 1
    assert _decisions(result)["a"].predicted_ttft_s == 2


@pytest.mark.parametrize("policy", ["density", "knapsack"])
def test_feasible_old_request_has_priority_when_aging_is_enabled(policy):
    candidates = [
        _candidate("old", estimated_remote_cost=0.1),
        _candidate("young", arrival_s=4, estimated_remote_cost=100),
    ]
    result = select_requests(
        policy,
        candidates,
        CapacitySnapshot(),
        _profile(starvation_s=3, max_running_requests=1),
        now_s=4,
    )
    assert result.selected_ids == ("old",)


@pytest.mark.parametrize("policy", ["density", "knapsack"])
def test_aging_never_forces_an_infeasible_request_local(policy):
    candidates = [
        _candidate("old", deadline_s=2),
        _candidate("young", arrival_s=4),
    ]
    result = select_requests(
        policy,
        candidates,
        CapacitySnapshot(),
        _profile(starvation_s=3, max_running_requests=1),
        now_s=4,
    )
    assert result.selected_ids == ("young",)


def test_knapsack_search_is_chunked_but_all_arrived_requests_are_considered():
    result = select_requests(
        "knapsack",
        [_candidate("a"), _candidate("b"), _candidate("c", estimated_remote_cost=100)],
        CapacitySnapshot(),
        _profile(knapsack_window=2),
        now_s=0,
    )
    assert result.selected_ids == ("a", "b", "c")
    assert result.search_truncated
    assert _decisions(result)["c"].reason == "admitted"


@pytest.mark.parametrize(
    "policy", ["all_local", "all_api", "concurrency", "fifo", "greedy", "density", "knapsack"]
)
def test_future_requests_do_not_enter_any_policy_or_change_decisions(policy):
    current = _candidate("current")
    future = _candidate("future", arrival_s=1, estimated_remote_cost=1000)
    result = select_requests(policy, [future, current], CapacitySnapshot(), _profile(), now_s=0)
    without_future = select_requests(policy, [current], CapacitySnapshot(), _profile(), now_s=0)
    assert result.selected_ids == without_future.selected_ids
    assert result.resulting_snapshot == without_future.resulting_snapshot
    assert _decisions(result)["current"] == without_future.decisions[0]
    assert _decisions(result)["future"].reason == "not_arrived"
    assert _decisions(result)["future"].predicted_ttft_s is None
    assert _decisions(result)["future"].predicted_tpot_s is None


def test_baselines_expose_deliberate_admission_differences():
    profile = _profile(max_running_requests=1, max_reserved_tokens=1, max_predicted_tpot_s=0)
    candidates = [_candidate("a", deadline_s=0), _candidate("b", deadline_s=0)]
    selections = {
        name: select_requests(name, candidates, CapacitySnapshot(), profile, now_s=0)
        for name in ("all_local", "all_api", "concurrency", "fifo")
    }
    assert selections["all_local"].selected_ids == ("a", "b")
    assert selections["concurrency"].selected_ids == ("a",)
    assert not selections["all_api"].selected_ids
    assert not selections["fifo"].selected_ids
    assert selections["all_local"].decisions[0].reason == "baseline_all_local"


@pytest.mark.parametrize("policy", ["all_local", "all_api", "fifo", "density", "knapsack"])
def test_empty_queue_is_side_effect_free(policy):
    snapshot = CapacitySnapshot(1, 100, 50, 10)
    result = select_requests(policy, [], snapshot, _profile(), now_s=0)
    assert not result.selected_ids
    assert not result.decisions
    assert not result.search_truncated
    assert result.resulting_snapshot == snapshot


def test_duplicate_ids_and_nonfinite_time_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        select_requests(
            "fifo", [_candidate("a"), _candidate("a")], CapacitySnapshot(), _profile(), 0
        )
    with pytest.raises(ValueError, match="now_s"):
        select_requests("fifo", [], CapacitySnapshot(), _profile(), float("nan"))
    with pytest.raises(ValueError, match="unknown policy"):
        select_requests("invalid", [], CapacitySnapshot(), _profile(), 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"uncached_prefill_tokens": 101},
        {"estimated_output_tokens": -1},
        {"estimated_output_tokens": 1.5},
        {"estimated_remote_cost": float("nan")},
        {"arrival_s": float("inf")},
        {"deadline_s": -1},
    ],
)
def test_invalid_online_features_fail_closed(changes):
    with pytest.raises(ValueError):
        _candidate("a", **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"prefill_tokens_per_s": 0},
        {"decode_tokens_per_s": float("inf")},
        {"max_predicted_tpot_s": -1},
        {"max_reserved_tokens": 1.5},
        {"max_running_requests": -1},
        {"knapsack_window": 21},
    ],
)
def test_invalid_profile_is_rejected(changes):
    with pytest.raises(ValueError):
        _profile(**changes)
