"""Daily quota manager with primal-dual shadow price computation.

Tracks request-level daily quota usage for S_Q (quota subscription) adapters
and computes the exponential shadow price ``theta_Q = L * (U/L)^z`` where
``z = used / Q`` is the fraction of the daily request quota consumed.  The
quota automatically resets at midnight in the configured timezone.

Quota is counted in **requests** (not tokens), matching the online knapsack
formulation in the paper: each request that is routed to S_Q consumes exactly
one quota slot.

This module is independent of ``experiment/`` -- the algorithm is reimplemented
here for production use without importing simulation code.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

from serving.utils.logging import get_logger

logger = get_logger(__name__)


class QuotaManager:
    """Production daily quota manager for RouteWise S_Q routing.

    Attributes:
        remaining: Requests remaining in today's quota.

    Args:
        config: ``RouteWiseConfig`` providing quota and shadow-price parameters.
    """

    def __init__(self, config) -> None:
        if config.daily_quota <= 0:
            raise ValueError(f"daily_quota must be positive, got {config.daily_quota}")
        self._daily_quota: int = config.daily_quota
        self._used_today: int = 0
        self._L: float = max(config.shadow_price_L_seed, 1e-9)
        self._U: float = max(config.shadow_price_U_seed, self._L)
        self._reset_tz: ZoneInfo = ZoneInfo(config.reset_timezone)
        self._last_reset_date: date = datetime.now(tz=self._reset_tz).date()

    @property
    def remaining(self) -> int:
        """Requests remaining in today's quota (non-negative)."""
        return max(0, self._daily_quota - self._used_today)

    def get_shadow_price(self) -> float:
        """Compute the current shadow price ``theta_Q``.

        Uses the exponential threshold function from online knapsack theory:

            ``theta_Q = L * (U / L) ^ z``

        where ``z = used / Q``, clamped to [0, 1].

        Returns:
            Shadow price in dollars.  Returns ``inf`` when the quota is
            fully exhausted.
        """
        self._maybe_reset()

        if self._used_today >= self._daily_quota:
            return float("inf")

        z = min(self._used_today / self._daily_quota, 1.0)
        return self._L * math.pow(self._U / self._L, z)

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
