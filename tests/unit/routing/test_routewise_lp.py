"""Tests for Layer 2 LP solver."""

from __future__ import annotations

import pytest

from routing.routewise.latency import ProviderProfile
from routing.routewise.lp_solver import (
    pre_filter_providers,
    solve_cost_budgeted_latency_lp,
    solve_provider_lp,
    solve_provider_lp_with_relaxation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(
    endpoint_id: str,
    latencies_ms: list[float],
    errors: int = 0,
    window_sec: float = 1000.0,
    timestamp: float = 100.0,
) -> ProviderProfile:
    """Create a ProviderProfile populated with sample data."""
    profile = ProviderProfile(endpoint_id=endpoint_id, window_sec=window_sec)
    for lat in latencies_ms:
        profile.record(timestamp, lat)
    for _ in range(errors):
        profile.record(timestamp, -1.0, error_type="timeout")
    return profile


# ---------------------------------------------------------------------------
# solve_provider_lp tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSolveProviderLP:
    def test_single_feasible(self):
        """Single provider meeting CDF target gets weight 1.0."""
        result = solve_provider_lp(
            endpoint_ids=["ep1"],
            cdfs=[0.99],
            costs=[1.0],
            error_rates=[0.0],
            target_cdf=0.99,
        )
        assert result is not None
        assert result["ep1"] == pytest.approx(1.0)

    def test_cheaper_provider_preferred(self):
        """Both meet SLO; cheaper provider gets all weight."""
        result = solve_provider_lp(
            endpoint_ids=["cheap", "expensive"],
            cdfs=[0.99, 0.99],
            costs=[1.0, 10.0],
            error_rates=[0.0, 0.0],
            target_cdf=0.99,
        )
        assert result is not None
        # Cheap provider should get weight ~1.0.
        assert result.get("cheap", 0.0) == pytest.approx(1.0, abs=0.01)

    def test_mixing_required(self):
        """Neither meets target alone; LP mixes to achieve target CDF."""
        # Provider A: CDF=0.8, cost=1.0
        # Provider B: CDF=1.0, cost=10.0
        # Target CDF=0.99.
        # Mix: pi_A * 0.8 + pi_B * 1.0 >= 0.99, pi_A + pi_B = 1
        # Need pi_B >= (0.99 - 0.8) / (1.0 - 0.8) = 0.95
        result = solve_provider_lp(
            endpoint_ids=["A", "B"],
            cdfs=[0.80, 1.0],
            costs=[1.0, 10.0],
            error_rates=[0.0, 0.0],
            target_cdf=0.99,
        )
        assert result is not None
        # Both providers should have non-zero weight.
        assert "A" in result or "B" in result
        # Verify mixing constraint satisfied:
        total_cdf = result.get("A", 0) * 0.80 + result.get("B", 0) * 1.0
        assert total_cdf >= 0.99 - 1e-6

    def test_infeasible_returns_none(self):
        """All CDFs below target -> None."""
        result = solve_provider_lp(
            endpoint_ids=["A", "B"],
            cdfs=[0.5, 0.7],
            costs=[1.0, 2.0],
            error_rates=[0.0, 0.0],
            target_cdf=0.99,
        )
        assert result is None


@pytest.mark.unit
class TestCostBudgetedLatencyLP:
    def test_cost_budgeted_lp_selects_fast_provider_when_budget_allows(self):
        weights, status = solve_cost_budgeted_latency_lp(
            endpoint_ids=["cheap", "fast"],
            mean_latencies_sec={"cheap": 1.0, "fast": 0.1},
            costs={"cheap": 1.0, "fast": 10.0},
            alpha=1.0,
        )

        assert status == "optimal"
        assert weights == {"fast": pytest.approx(1.0)}

    def test_cost_budgeted_lp_respects_alpha_budget(self):
        weights, status = solve_cost_budgeted_latency_lp(
            endpoint_ids=["cheap", "fast"],
            mean_latencies_sec={"cheap": 1.0, "fast": 0.1},
            costs={"cheap": 1.0, "fast": 10.0},
            alpha=0.5,
        )

        assert status == "optimal"
        actual_cost = sum(weights[eid] * {"cheap": 1.0, "fast": 10.0}[eid] for eid in weights)
        assert actual_cost <= 5.5 + 1e-6
        assert 0.0 < weights.get("fast", 0.0) < 1.0

    def test_error_penalty(self):
        """kappa > 0 shifts weights away from high-error providers."""
        # Both providers have CDF=1.0, same cost.
        # Provider A: error_rate=0.5
        # Provider B: error_rate=0.0
        # With kappa=10, effective cost of A = 1.0 * (1 + 10*0.5) = 6.0
        # B = 1.0 * (1 + 10*0.0) = 1.0.  B should get all weight.
        result = solve_provider_lp(
            endpoint_ids=["A", "B"],
            cdfs=[1.0, 1.0],
            costs=[1.0, 1.0],
            error_rates=[0.5, 0.0],
            target_cdf=0.99,
            kappa=10.0,
        )
        assert result is not None
        assert result.get("B", 0.0) == pytest.approx(1.0, abs=0.01)

    def test_empty_endpoints(self):
        """Empty endpoint list returns None."""
        result = solve_provider_lp([], [], [], [], target_cdf=0.99)
        assert result is None


# ---------------------------------------------------------------------------
# solve_provider_lp_with_relaxation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRelaxation:
    def test_original_slo_feasible(self):
        """Returns 'optimal' when original SLO is feasible."""
        profiles = {
            "ep1": _make_profile("ep1", [200.0] * 20),  # 200ms all
        }
        costs = {"ep1": 1.0}

        weights, status = solve_provider_lp_with_relaxation(
            endpoint_ids=["ep1"],
            profiles=profiles,
            costs=costs,
            slo_sec=1.0,  # 1s SLO; all samples at 200ms.
            current_time=100.0,
        )
        assert status == "optimal"
        assert weights["ep1"] == pytest.approx(1.0)

    def test_relaxed_slo(self):
        """Returns 'relaxed_1.2x' when original SLO fails but 1.2x works."""
        # Samples at 1100ms.  SLO = 1.0s fails.  1.2s = 1200ms works.
        profiles = {
            "ep1": _make_profile("ep1", [1100.0] * 20),
        }
        costs = {"ep1": 1.0}

        weights, status = solve_provider_lp_with_relaxation(
            endpoint_ids=["ep1"],
            profiles=profiles,
            costs=costs,
            slo_sec=1.0,
            current_time=100.0,
        )
        assert status == "relaxed_1.2x"
        assert "ep1" in weights

    def test_best_effort(self):
        """All relaxations fail -> picks max-CDF provider."""
        # Samples at 10000ms.  No SLO relaxation will work.
        profiles = {
            "ep1": _make_profile("ep1", [10000.0] * 20),
            "ep2": _make_profile("ep2", [8000.0] * 20),
        }
        costs = {"ep1": 1.0, "ep2": 2.0}

        weights, status = solve_provider_lp_with_relaxation(
            endpoint_ids=["ep1", "ep2"],
            profiles=profiles,
            costs=costs,
            slo_sec=1.0,
            current_time=100.0,
        )
        assert status == "best_effort"
        assert len(weights) == 1

    def test_no_providers(self):
        """Empty endpoint list returns no_providers."""
        weights, status = solve_provider_lp_with_relaxation(
            endpoint_ids=[],
            profiles={},
            costs={},
            slo_sec=1.0,
            current_time=100.0,
        )
        assert status == "no_providers"
        assert weights == {}


# ---------------------------------------------------------------------------
# pre_filter_providers tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPreFilter:
    def test_high_error_rate_filtered(self):
        """Providers with error_rate > threshold are filtered out."""
        profiles = {
            "healthy": _make_profile("healthy", [200.0] * 20),
            "broken": _make_profile("broken", [200.0] * 10, errors=10),
        }
        # "broken" has 50% error rate, exceeds 5% threshold.
        eligible = pre_filter_providers(profiles, current_time=100.0)
        assert "healthy" in eligible
        assert "broken" not in eligible

    def test_low_cdf_filtered(self):
        """Providers with CDF < threshold at min SLO are filtered out."""
        profiles = {
            "fast": _make_profile("fast", [200.0] * 20),  # CDF(1s) = 1.0
            "slow": _make_profile("slow", [5000.0] * 20),  # CDF(1s) = 0.0
        }
        eligible = pre_filter_providers(profiles, current_time=100.0)
        assert "fast" in eligible
        assert "slow" not in eligible


@pytest.mark.unit
def test_provider_profile_mean_ttft_sec():
    profile = _make_profile("ep", [100.0, 300.0, 500.0], timestamp=100.0)

    assert profile.mean_ttft_sec(100.0) == pytest.approx(0.3)

    def test_healthy_pass(self):
        """All healthy providers pass the filter."""
        profiles = {
            "ep1": _make_profile("ep1", [200.0] * 20),
            "ep2": _make_profile("ep2", [300.0] * 20),
        }
        eligible = pre_filter_providers(profiles, current_time=100.0)
        assert set(eligible) == {"ep1", "ep2"}

    def test_empty_profiles(self):
        """Empty profiles return empty list."""
        eligible = pre_filter_providers({}, current_time=100.0)
        assert eligible == []
