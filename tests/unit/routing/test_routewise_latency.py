"""Tests for RouteWise latency profiling."""

from __future__ import annotations

import pytest

from routing.routewise.latency import ProviderProfile

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

    def test_cdf_ignores_success_with_nonpositive_ttft(self):
        """A success recorded without a TTFT must not deflate the CDF."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=1000.0)
        now = 100.0

        # 3 timed successes within 0.5s, 2 timed successes above it.
        for _ in range(3):
            profile.record(now, 100.0)
        for _ in range(2):
            profile.record(now, 900.0)
        # 2 errors.
        profile.record(now, -1.0, error_type="timeout")
        profile.record(now, -1.0, error_type="server_error")
        # One success with no TTFT (instrumentation missing): must be ignored,
        # not counted as a real outcome that drags the CDF down.
        profile.record(now, 0.0)

        # F(0.5) = within / (successes + errors) = 3 / (5 + 2).
        assert profile.cdf_at(0.5, now) == pytest.approx(3 / 7)

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

    def test_sample_count_respects_window(self):
        """sample_count only counts samples within the time window."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=100.0)

        profile.record(10.0, 100.0)  # Old
        profile.record(20.0, 200.0)  # Old
        profile.record(150.0, 300.0)  # Recent

        # At t=200, window is [100, 200].
        assert profile.sample_count(200.0) == 1

    def test_max_samples_evicts_oldest_outcomes(self):
        """Profile storage is bounded by the configured outcome cap."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=1000.0, max_samples=3)

        for i in range(5):
            profile.record(100.0 + i, (i + 1) * 100.0)

        assert profile.max_samples == 3
        assert len(profile._events) == 3
        assert profile.sample_count(200.0) == 3
        assert [ttft for _t, ttft, _e in profile._events] == [300.0, 400.0, 500.0]

    def test_max_samples_bounds_successes_and_errors_together(self):
        """Success and error accounting use the same bounded outcome window."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=1000.0, max_samples=4)
        now = 100.0

        profile.record(now + 0, 100.0)
        profile.record(now + 1, -1.0, error_type="timeout")
        profile.record(now + 2, 200.0)
        profile.record(now + 3, -1.0, error_type="rate_limit")
        profile.record(now + 4, 300.0)

        assert len(profile._events) == 4
        assert profile.sample_count(now + 4) == 2
        assert profile.total_count(now + 4) == 4
        assert profile.error_rate(now + 4) == pytest.approx(0.5)

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

    def test_mean_with_errors_uses_synthetic_penalty(self):
        """Failed attempts enter the latency profile as synthetic penalty samples."""
        profile = ProviderProfile(endpoint_id="ep1", window_sec=1000.0)
        now = 100.0

        profile.record(now, 100.0)
        profile.record(now, -1.0, error_type="timeout")

        assert profile.total_count(now) == 2
        assert profile.mean_with_errors_sec(
            now,
            error_penalty_ms=60_000.0,
        ) == pytest.approx(30.05)

    def test_empty_profile(self):
        """Empty profile returns safe defaults."""
        profile = ProviderProfile(endpoint_id="ep1")
        now = 100.0

        assert profile.cdf_at(1.0, now) == 0.0
        assert profile.mean_with_errors_sec(now, error_penalty_ms=60_000.0) is None
        assert profile.error_rate(now) == 0.0
        assert profile.sample_count(now) == 0
        assert profile.total_count(now) == 0

    def test_invalid_max_samples_is_coerced_to_one(self):
        profile = ProviderProfile(endpoint_id="ep1", max_samples=0)

        profile.record(100.0, 100.0)
        profile.record(101.0, 200.0)

        assert profile.max_samples == 1
        assert len(profile._events) == 1
        assert profile.sample_count(101.0) == 1
