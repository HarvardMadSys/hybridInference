"""Tests for current RouteWise body-router primitives."""

from __future__ import annotations

import pytest
from routewise.core import (
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
    """Any non-empty pool reports observed P_lower / P_upper directly."""
    estimator = CostEnvelopeEstimator(lower_percentile=0, upper_percentile=100)

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
    )

    estimator.observe("m", 0.05, now=0.0)
    estimator.observe("m", 0.20, now=1.0)
    assert estimator.snapshot("m", now=2.0) is not None

    # Advance past the window: all samples drop and the pool is uncalibrated.
    assert estimator.snapshot("m", now=100.0) is None


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
