"""Tests for RouteWise daily quota manager."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from routing.routewise.config import RouteWiseConfig
from routing.routewise.quota import QuotaManager


def _make_config(**overrides) -> RouteWiseConfig:
    """Create a RouteWiseConfig with optional overrides."""
    defaults = {
        "daily_quota": 1000,
        "shadow_price_L_seed": 0.001,
        "shadow_price_U_seed": 0.500,
        "reset_timezone": "UTC",
    }
    defaults.update(overrides)
    return RouteWiseConfig(**defaults)


@pytest.mark.unit
class TestQuotaManager:
    def test_initial_remaining(self):
        """Full quota available at start."""
        mgr = QuotaManager(_make_config(daily_quota=5000))
        assert mgr.remaining == 5000

    def test_consume_reduces_remaining(self):
        """Each consume() decrements the remaining quota by 1 request."""
        mgr = QuotaManager(_make_config(daily_quota=1000))
        mgr.consume()
        assert mgr.remaining == 999
        mgr.consume()
        assert mgr.remaining == 998

    def test_remaining_never_negative(self):
        """remaining is clamped to 0 even if usage exceeds quota."""
        mgr = QuotaManager(_make_config(daily_quota=3))
        mgr.consume()
        mgr.consume()
        mgr.consume()
        mgr.consume()  # Over-consume.
        assert mgr.remaining == 0

    def test_shadow_price_at_zero_usage(self):
        """At zero usage, shadow price equals L_seed."""
        mgr = QuotaManager(
            _make_config(
                shadow_price_L_seed=0.001,
                shadow_price_U_seed=0.500,
            )
        )
        price = mgr.get_shadow_price()
        assert price == pytest.approx(0.001)

    def test_shadow_price_at_full_usage(self):
        """At full usage, shadow price equals inf."""
        cfg = _make_config(daily_quota=3)
        mgr = QuotaManager(cfg)
        mgr.consume()
        mgr.consume()
        mgr.consume()
        price = mgr.get_shadow_price()
        assert price == float("inf")

    def test_shadow_price_increases_with_usage(self):
        """Shadow price monotonically increases as quota is consumed."""
        cfg = _make_config(daily_quota=10)
        mgr = QuotaManager(cfg)

        prices = []
        for _ in range(10):
            prices.append(mgr.get_shadow_price())
            mgr.consume()

        for i in range(1, len(prices)):
            assert prices[i] >= prices[i - 1], (
                f"Price at step {i} ({prices[i]}) < step {i - 1} ({prices[i - 1]})"
            )

    def test_shadow_price_at_half_usage(self):
        """At 50% usage, theta_Q = L * (U/L)^0.5 = sqrt(L*U)."""
        import math

        L, U = 0.001, 0.500
        cfg = _make_config(
            daily_quota=100,
            shadow_price_L_seed=L,
            shadow_price_U_seed=U,
        )
        mgr = QuotaManager(cfg)
        for _ in range(50):
            mgr.consume()
        expected = math.sqrt(L * U)
        assert mgr.get_shadow_price() == pytest.approx(expected, rel=1e-6)

    def test_daily_reset(self):
        """Usage resets when date crosses midnight in reset_tz."""
        cfg = _make_config(daily_quota=100, reset_timezone="UTC")
        mgr = QuotaManager(cfg)
        for _ in range(60):
            mgr.consume()
        assert mgr.remaining == 40

        # Simulate date rollover by patching datetime.now.
        tomorrow = datetime.now(tz=ZoneInfo("UTC")) + timedelta(days=1)
        with patch("routing.routewise.quota.datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            price = mgr.get_shadow_price()
            assert mgr.remaining == 100
            assert price == pytest.approx(0.001)

    def test_consume_triggers_reset_check(self):
        """consume() also triggers date reset check."""
        cfg = _make_config(daily_quota=500, reset_timezone="UTC")
        mgr = QuotaManager(cfg)
        for _ in range(300):
            mgr.consume()
        assert mgr.remaining == 200

        tomorrow = datetime.now(tz=ZoneInfo("UTC")) + timedelta(days=1)
        with patch("routing.routewise.quota.datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mgr.consume()
            # Reset happened, then consumed 1.
            assert mgr.remaining == 499
