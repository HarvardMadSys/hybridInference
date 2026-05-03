"""Tests for Layer 2 latency profiling and SWRR sampling."""

from __future__ import annotations

import pytest

from routing.routewise.latency import ProviderProfile, ShadowHedgeDecision, SWRRSampler

# ---------------------------------------------------------------------------
# ProviderProfile tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderProfile:
    def test_cdf_basic(self):
        """Known samples produce correct CDF values."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=1000.0)
        now = 100.0

        # Add 10 samples: 100ms, 200ms, ..., 1000ms (all successes).
        for i in range(1, 11):
            profile.record(now, i * 100.0)

        # CDF at 0.5s (500ms): 5/10 successes below threshold.
        # success_rate = 1.0, F_success = 5/10 = 0.5.
        assert profile.cdf_at(0.5, now) == pytest.approx(0.5)

        # CDF at 1.0s (1000ms): all 10 below threshold.
        assert profile.cdf_at(1.0, now) == pytest.approx(1.0)

        # CDF at 0.05s (50ms): none below threshold.
        assert profile.cdf_at(0.05, now) == pytest.approx(0.0)

    def test_cdf_with_errors_infinity_mode(self):
        """Errors reduce CDF: F(L) = success_rate * F_success(L)."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=1000.0)
        now = 100.0

        # 8 successful requests at 100ms each.
        for _ in range(8):
            profile.record(now, 100.0)

        # 2 errors (they record events but not latency samples).
        profile.record(now, -1.0, error_type="timeout")
        profile.record(now, -1.0, error_type="server_error")

        # success_rate = 8/10 = 0.8
        # F_success(0.5) = 8/8 = 1.0 (all successes are below 0.5s)
        # F(0.5) = 0.8 * 1.0 = 0.8
        assert profile.cdf_at(0.5, now) == pytest.approx(0.8)

    def test_window_pruning(self):
        """Old samples are excluded after the window."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=100.0)

        # Sample at t=50 (old).
        profile.record(50.0, 200.0)
        # Sample at t=160 (recent).
        profile.record(160.0, 300.0)

        # At t=200, window is [100, 200].  Only t=160 sample is in window.
        assert profile.sample_count(200.0) == 1
        assert profile.cdf_at(0.5, 200.0) == pytest.approx(1.0)

    def test_percentile(self):
        """Percentile computation matches manual calculation."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=1000.0)
        now = 100.0

        # Add 5 samples: 100, 200, 300, 400, 500 ms.
        for v in [100.0, 200.0, 300.0, 400.0, 500.0]:
            profile.record(now, v)

        # p50 (median): index = 0.5 * 4 = 2.0 -> samples[2] = 0.3s
        assert profile.percentile(50, now) == pytest.approx(0.3)

        # p0: index = 0 -> samples[0] = 0.1s
        assert profile.percentile(0, now) == pytest.approx(0.1)

        # p100: index = 4 -> samples[4] = 0.5s
        assert profile.percentile(100, now) == pytest.approx(0.5)

    def test_sample_count_respects_window(self):
        """sample_count only counts samples within the time window."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=100.0)

        profile.record(10.0, 100.0)  # Old
        profile.record(20.0, 200.0)  # Old
        profile.record(150.0, 300.0)  # Recent

        # At t=200, window is [100, 200].
        assert profile.sample_count(200.0) == 1

    def test_error_rate(self):
        """Error rate is correctly computed."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=1000.0)
        now = 100.0

        # 7 successes, 3 errors.
        for _ in range(7):
            profile.record(now, 100.0)
        for _ in range(3):
            profile.record(now, -1.0, error_type="timeout")

        assert profile.error_rate(now) == pytest.approx(0.3)

    def test_empty_profile(self):
        """Empty profile returns safe defaults."""
        profile = ProviderProfile(endpoint_id="ep1")
        now = 100.0

        assert profile.cdf_at(1.0, now) == 0.0
        assert profile.percentile(50, now) == float("inf")
        assert profile.error_rate(now) == 0.0
        assert profile.sample_count(now) == 0


# ---------------------------------------------------------------------------
# SWRRSampler tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSWRRSampler:
    def test_single_provider(self):
        """Single provider always selected."""
        sampler = SWRRSampler(alpha=0.3)
        sampler.update_weights({"A": 1.0})

        for _ in range(10):
            assert sampler.sample() == "A"

    def test_proportional_distribution(self):
        """Over many samples, distribution approximates target weights."""
        sampler = SWRRSampler(alpha=1.0)  # alpha=1 for immediate convergence.
        sampler.update_weights({"A": 0.7, "B": 0.3})

        counts = {"A": 0, "B": 0}
        n = 1000
        for _ in range(n):
            selected = sampler.sample()
            counts[selected] += 1

        # SWRR should exactly match weights.
        assert counts["A"] / n == pytest.approx(0.7, abs=0.02)
        assert counts["B"] / n == pytest.approx(0.3, abs=0.02)

    def test_exponential_smoothing(self):
        """Weight update blends old and new via alpha."""
        sampler = SWRRSampler(alpha=0.5)
        sampler.update_weights({"A": 1.0})

        # Old weights: A=1.0. New: A=0.4, B=0.6.
        # Smoothed: A = 0.5*0.4 + 0.5*1.0 = 0.7; B = 0.5*0.6 = 0.3.
        # After normalization: A=0.7, B=0.3.
        sampler.update_weights({"A": 0.4, "B": 0.6})

        weights = sampler.get_weights()
        assert weights["A"] == pytest.approx(0.7, abs=0.01)
        assert weights["B"] == pytest.approx(0.3, abs=0.01)

    def test_provider_removal(self):
        """Providers with negligible weight after smoothing are removed."""
        sampler = SWRRSampler(alpha=1.0)
        sampler.update_weights({"A": 0.9, "B": 0.1})

        # Now update with B having weight 0 -> after smoothing with alpha=1.0,
        # B = 0.0 which is < 0.001, so B should be removed.
        sampler.update_weights({"A": 1.0, "B": 0.0})

        weights = sampler.get_weights()
        assert "B" not in weights
        assert "A" in weights

    def test_empty_sampler(self):
        """Empty sampler returns None."""
        sampler = SWRRSampler()
        assert sampler.sample() is None


# ---------------------------------------------------------------------------
# ShadowHedgeDecision tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShadowHedgeDecision:
    def test_dataclass_creation(self):
        """ShadowHedgeDecision can be instantiated with all fields."""
        decision = ShadowHedgeDecision(
            model_id="test-model",
            primary_endpoint="ep1",
            backup_endpoint="ep2",
            hedge_threshold_sec=0.5,
            reason="hedge_warranted",
            timestamp=1000.0,
        )
        assert decision.model_id == "test-model"
        assert decision.primary_endpoint == "ep1"
        assert decision.backup_endpoint == "ep2"
        assert decision.hedge_threshold_sec == 0.5
        assert decision.reason == "hedge_warranted"
