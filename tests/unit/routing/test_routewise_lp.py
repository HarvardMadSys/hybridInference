"""Tests for Layer 2 LP solver."""

from __future__ import annotations

import pytest

from routing.routewise.latency import ProviderProfile
from routing.routewise.lp_solver import solve_cost_budgeted_latency_lp

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
# solve_cost_budgeted_latency_lp tests
# ---------------------------------------------------------------------------


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

    def test_no_finite_providers(self):
        """No finite-cost/latency endpoint returns no_providers."""
        weights, status = solve_cost_budgeted_latency_lp(
            endpoint_ids=["ep1"],
            mean_latencies_sec={"ep1": float("inf")},
            costs={"ep1": float("inf")},
            alpha=0.5,
        )
        assert status == "no_providers"
        assert weights == {}


@pytest.mark.unit
def test_provider_profile_mean_ttft_sec():
    profile = _make_profile("ep", [100.0, 300.0, 500.0], timestamp=100.0)

    assert profile.mean_ttft_sec(100.0) == pytest.approx(0.3)
