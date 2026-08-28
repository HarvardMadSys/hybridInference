"""Tests for RouteWise cost-layer primitives (effective cost, envelope, LP)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from llm_routewise.core import (
    BudgetLPCandidate,
    cost_tiebroken_objective,
    quota_effective_cost,
    solve_budget_lp,
)

from routing.routewise.effective_cost import api_request_cost_usd, quota_shadow_price_usd
from routing.routewise.envelope import CostEnvelopeEstimator
from routing.routewise.lp import LPCandidate, solve_cost_budgeted_mean_ttft


@pytest.mark.unit
def test_api_request_cost_uses_cold_cache_assumption():
    cost = api_request_cost_usd(
        prompt_tokens=1000,
        predicted_output_tokens=1000,
        input_price_per_m=10.0,
        output_price_per_m=10.0,
    )

    assert cost == pytest.approx(0.02)


@pytest.mark.unit
def test_quota_shadow_price_interpolates_lu():
    assert quota_shadow_price_usd(used_fraction=0.0, lower=0.01, upper=1.0) == pytest.approx(0.01)
    assert quota_shadow_price_usd(used_fraction=1.0, lower=0.01, upper=1.0) == pytest.approx(
        quota_effective_cost(1.0, L=0.01, U=1.0)
    )
    assert quota_shadow_price_usd(used_fraction=0.5, lower=0.01, upper=1.0) == pytest.approx(0.1)


@pytest.mark.unit
def test_envelope_returns_none_until_first_observation():
    """No seed fallback: an empty pool yields ``None`` (uncalibrated)."""
    estimator = CostEnvelopeEstimator(lower_percentile=0, upper_percentile=100)

    assert estimator.snapshot("m", now=1.0) is None


@pytest.mark.unit
def test_envelope_returns_observed_percentiles_once_calibrated():
    """Any pool past the min-sample gate reports observed P_lower / P_upper."""
    estimator = CostEnvelopeEstimator(lower_percentile=0, upper_percentile=100, min_samples=1)

    estimator.observe("m", 0.02, now=1.0)
    estimator.observe("m", 0.10, now=2.0)
    estimator.observe("m", 0.50, now=3.0)
    snap = estimator.snapshot("m", now=3.0)

    assert snap is not None
    assert snap.sample_count == 3
    assert snap.lower == pytest.approx(0.02)
    assert snap.upper == pytest.approx(0.50)


@pytest.mark.unit
def test_envelope_returns_none_after_window_evicts_all_samples():
    """Once the rolling window prunes every sample, snapshot returns ``None``."""
    estimator = CostEnvelopeEstimator(
        lower_percentile=10,
        upper_percentile=90,
        window_sec=10.0,
        min_samples=1,
    )

    estimator.observe("m", 0.05, now=0.0)
    estimator.observe("m", 0.20, now=1.0)
    assert estimator.snapshot("m", now=2.0) is not None

    # Advance past the window: all samples drop and the pool is uncalibrated.
    assert estimator.snapshot("m", now=100.0) is None


@pytest.mark.unit
def test_envelope_min_samples_gates_calibration():
    """Below ``min_samples`` the pool stays uncalibrated; at it, it calibrates."""
    estimator = CostEnvelopeEstimator(min_samples=30)
    for i in range(29):
        estimator.observe("m", 0.001 + i * 0.0001, now=float(i))
    assert estimator.snapshot("m", now=29.0) is None
    assert estimator.sample_count("m", now=29.0) == 29

    estimator.observe("m", 0.01, now=29.5)
    snap = estimator.snapshot("m", now=30.0)
    assert snap is not None
    assert snap.sample_count == 30


@pytest.mark.unit
def test_envelope_floor_fallback_keeps_bounded_ratio():
    """Degenerate windows (all-equal samples) fall back to L = U * 1e-3.

    Without the paper's floor fallback, P10 == P90 would collapse the quota
    shadow-price curve into a constant (no rationing).
    """
    estimator = CostEnvelopeEstimator(min_samples=1)
    for i in range(40):
        estimator.observe("m", 0.002, now=float(i))
    snap = estimator.snapshot("m", now=40.0)
    assert snap is not None
    assert snap.upper == pytest.approx(0.002)
    assert snap.lower == pytest.approx(0.002 * 1e-3)


@pytest.mark.unit
def test_envelope_supports_concurrent_observe_and_snapshot():
    estimator = CostEnvelopeEstimator(
        lower_percentile=0,
        upper_percentile=100,
        window_sec=10_000.0,
    )

    def observe_worker(offset: int) -> None:
        for idx in range(200):
            estimator.observe("m", 0.01 + ((offset + idx) % 50) * 0.001, now=float(offset + idx))

    def snapshot_worker() -> None:
        for _ in range(200):
            snap = estimator.snapshot("m", now=500.0)
            if snap is not None:
                assert snap.lower <= snap.upper
                assert snap.sample_count > 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            *(pool.submit(observe_worker, worker * 1000) for worker in range(4)),
            *(pool.submit(snapshot_worker) for _ in range(4)),
        ]
        for future in futures:
            future.result()

    snap = estimator.snapshot("m", now=500.0)
    assert snap is not None
    assert snap.sample_count == 800


@pytest.mark.unit
def test_body_lp_mixes_fast_expensive_with_slow_cheap_at_budget():
    solution = solve_cost_budgeted_mean_ttft(
        [
            LPCandidate("slow-cheap", cost_usd=1.0, mean_ttft_sec=10.0),
            LPCandidate("fast-expensive", cost_usd=3.0, mean_ttft_sec=1.0),
        ],
        alpha=0.5,
    )

    assert solution.status == "optimal"
    assert solution.budget_usd == pytest.approx(2.0)
    assert solution.weights["fast-expensive"] == pytest.approx(0.5)
    assert solution.weights["slow-cheap"] == pytest.approx(0.5)


@pytest.mark.unit
def test_body_lp_wrapper_matches_routewise_core_mapping():
    candidates = [
        LPCandidate("slow-cheap", cost_usd=1.0, mean_ttft_sec=10.0),
        LPCandidate("fast-expensive", cost_usd=3.0, mean_ttft_sec=1.0),
    ]
    objective_ms = cost_tiebroken_objective(
        [candidate.mean_ttft_sec * 1000.0 for candidate in candidates],
        [candidate.cost_usd for candidate in candidates],
    )
    core_result = solve_budget_lp(
        [
            BudgetLPCandidate(
                name=candidate.endpoint_id,
                objective=objective_ms[index],
                effective_cost=candidate.cost_usd,
            )
            for index, candidate in enumerate(candidates)
        ],
        budget=2.0,
    )

    solution = solve_cost_budgeted_mean_ttft(candidates, alpha=0.5)

    assert core_result.feasible
    assert solution.weights == pytest.approx(core_result.weights)
    assert solution.budget_usd == pytest.approx(core_result.budget)


@pytest.mark.unit
def test_body_lp_uses_simulator_cost_tiebreak_for_near_equal_latency():
    solution = solve_cost_budgeted_mean_ttft(
        [
            LPCandidate("slow-cheap", cost_usd=1.0, mean_ttft_sec=1.0000005),
            LPCandidate("fast-expensive", cost_usd=3.0, mean_ttft_sec=1.0),
        ],
        alpha=1.0,
    )

    assert solution.status == "optimal"
    assert solution.weights == {"slow-cheap": 1.0}
