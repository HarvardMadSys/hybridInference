"""Tests for the growth-card trend arithmetic."""

from __future__ import annotations

import pytest

from serving.analytics.growth import linear_slope, summarize


class TestLinearSlope:
    def test_perfect_ramp_recovers_its_step(self):
        # +3 per bucket, so the fitted slope is exactly 3.
        assert linear_slope([10, 13, 16, 19, 22]) == pytest.approx(3.0)

    def test_flat_series_has_no_slope(self):
        assert linear_slope([7, 7, 7, 7]) == pytest.approx(0.0)

    def test_decline_is_negative(self):
        assert linear_slope([20, 15, 10, 5]) == pytest.approx(-5.0)

    def test_fit_ignores_a_single_spike(self):
        # A lone outlier moves the fit but must not define it: the underlying
        # ramp is +1/day, and one 100 on day 3 should not read as +25/day.
        slope = linear_slope([1, 2, 3, 100, 5, 6, 7])
        assert 0 < slope < 6

    @pytest.mark.parametrize("values", [[], [42]])
    def test_too_short_to_fit(self, values):
        assert linear_slope(values) == 0.0


class TestSummarize:
    def test_splits_into_equal_halves(self):
        summary = summarize([10, 10, 20, 20])
        assert summary.compare_days == 2
        assert summary.previous_avg == pytest.approx(10.0)
        assert summary.recent_avg == pytest.approx(20.0)
        assert summary.change_pct == pytest.approx(1.0)

    def test_odd_length_drops_the_middle_day(self):
        # 5 days -> 2 per half; the middle 999 belongs to neither, so it cannot
        # tilt the comparison by landing in whichever half is longer.
        summary = summarize([10, 10, 999, 20, 20])
        assert summary.compare_days == 2
        assert summary.previous_avg == pytest.approx(10.0)
        assert summary.recent_avg == pytest.approx(20.0)

    def test_growth_from_zero_has_no_percentage(self):
        summary = summarize([0, 0, 5, 9])
        assert summary.previous_avg == 0.0
        assert summary.recent_avg == pytest.approx(7.0)
        assert summary.change_pct is None

    def test_decline_reports_negative_change(self):
        summary = summarize([100, 100, 75, 75])
        assert summary.change_pct == pytest.approx(-0.25)

    def test_single_day_reports_itself_without_a_trend(self):
        summary = summarize([12.0])
        assert summary.slope_per_day == 0.0
        assert summary.recent_avg == pytest.approx(12.0)
        assert summary.previous_avg == 0.0
        assert summary.change_pct is None
        assert summary.compare_days == 0

    def test_empty_series_is_all_zero(self):
        summary = summarize([])
        assert summary.slope_per_day == 0.0
        assert summary.recent_avg == 0.0
        assert summary.change_pct is None
