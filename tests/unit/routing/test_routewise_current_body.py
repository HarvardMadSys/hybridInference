"""Tests for current RouteWise body-router primitives."""

from __future__ import annotations

import pytest

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
    assert quota_shadow_price_usd(used_fraction=1.0, lower=0.01, upper=1.0) == pytest.approx(1.0)
    assert quota_shadow_price_usd(used_fraction=0.5, lower=0.01, upper=1.0) == pytest.approx(0.1)


@pytest.mark.unit
def test_envelope_uses_seed_until_min_samples_then_percentiles():
    estimator = CostEnvelopeEstimator(
        lower_seed=0.001,
        upper_seed=0.5,
        min_samples=3,
        min_ratio=1.0,
        lower_percentile=0,
        upper_percentile=100,
    )

    estimator.observe("m", 0.02, now=1.0)
    assert estimator.snapshot("m", now=1.0).source == "seed"

    estimator.observe("m", 0.10, now=2.0)
    estimator.observe("m", 0.50, now=3.0)
    snap = estimator.snapshot("m", now=3.0)

    assert snap.source == "observed"
    assert snap.lower == pytest.approx(0.02)
    assert snap.upper == pytest.approx(0.50)


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
