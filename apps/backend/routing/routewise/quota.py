"""Daily quota request counter for RouteWise S_Q routing.

Tracks request-level daily quota usage for quota-subscription adapters when
the provider does not expose a queryable quota dashboard. The shadow-price
math itself lives in :func:`effective_cost.quota_shadow_price_usd`, which
reads ``L/U`` from the workload :class:`CostEnvelopeEstimator`; this manager
only owns the depletion counter and the timezone-aware daily reset.

Quota is counted in **requests** (not tokens), matching the online knapsack
formulation in the paper: each request routed to S_Q consumes one slot.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from serving.utils.logging import get_logger

logger = get_logger(__name__)


class QuotaManager:
    """Daily request-quota counter for RouteWise S_Q routing.

    Attributes:
        remaining: Requests remaining in today's quota.

    Args:
        config: ``RouteWiseConfig`` providing ``daily_quota`` and
            ``reset_timezone``.
    """

    def __init__(self, config) -> None:
        if config.daily_quota <= 0:
            raise ValueError(f"daily_quota must be positive, got {config.daily_quota}")
        self._daily_quota: int = config.daily_quota
        self._used_today: int = 0
        self._reset_tz: ZoneInfo = ZoneInfo(config.reset_timezone)
        self._last_reset_date: date = datetime.now(tz=self._reset_tz).date()

    @property
    def remaining(self) -> int:
        """Requests remaining in today's quota (non-negative)."""
        self._maybe_reset()
        return max(0, self._daily_quota - self._used_today)

    @property
    def daily_quota(self) -> int:
        """Configured daily request quota."""
        return self._daily_quota

    @property
    def used_today(self) -> int:
        """Requests consumed in the current reset window."""
        self._maybe_reset()
        return self._used_today

    @property
    def used_fraction(self) -> float:
        """Fraction of today's quota consumed, clamped to [0, 1]."""
        self._maybe_reset()
        return min(max(self._used_today / self._daily_quota, 0.0), 1.0)

    def consume(self) -> None:
        """Consume one request slot from today's quota.

        Automatically resets the counter if the calendar date (in the
        configured timezone) has changed since the last reset.
        """
        self._maybe_reset()
        self._used_today += 1

    def _maybe_reset(self) -> None:
        """Reset ``_used_today`` to 0 if the calendar date has rolled over."""
        today = datetime.now(tz=self._reset_tz).date()
        if today > self._last_reset_date:
            logger.info(
                "QuotaManager daily reset: %s -> %s (used=%d)",
                self._last_reset_date,
                today,
                self._used_today,
            )
            self._used_today = 0
            self._last_reset_date = today
