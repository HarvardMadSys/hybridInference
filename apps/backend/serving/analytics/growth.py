"""Trend arithmetic behind the admin growth cards.

Kept free of database and HTTP concerns so the slope maths is unit tested on its
own: one plain list of daily values in, the card's headline numbers out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class TrendSummary:
    """How one daily series moved across the range.

    ``slope_per_day`` is in units of the series per day: for a daily-active-user
    series, "the DAU count rises by N users each day".
    """

    slope_per_day: float
    recent_avg: float
    previous_avg: float
    # None rather than infinity when the older half is flat zero.
    change_pct: float | None
    # Days per half, so the card can label the badge ("vs previous 15d").
    compare_days: int


def linear_slope(values: Sequence[float]) -> float:
    """Return the ordinary-least-squares slope of ``values`` over 0..n-1.

    One value per day in, "units per day" out. Fewer than two points have no
    defined slope and yield 0.0.
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    numerator = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    # Sum of squared deviations of 0..n-1, always > 0 once n >= 2.
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    return numerator / denominator


def summarize(values: Sequence[float]) -> TrendSummary:
    """Summarize one daily series: fitted slope plus a recent-vs-older comparison.

    The comparison splits the range into two equal halves and compares their
    means. An odd-length range drops its middle day rather than letting one half
    carry an extra day, which would tilt the percentage on its own.
    """
    n = len(values)
    if n < 2:
        only = float(values[0]) if n == 1 else 0.0
        return TrendSummary(
            slope_per_day=0.0,
            recent_avg=only,
            previous_avg=0.0,
            change_pct=None,
            compare_days=0,
        )

    half = n // 2
    previous = values[:half]
    recent = values[-half:]
    previous_avg = sum(previous) / half
    recent_avg = sum(recent) / half
    change_pct = (recent_avg - previous_avg) / previous_avg if previous_avg > 0 else None

    return TrendSummary(
        slope_per_day=linear_slope(values),
        recent_avg=recent_avg,
        previous_avg=previous_avg,
        change_pct=change_pct,
        compare_days=half,
    )
