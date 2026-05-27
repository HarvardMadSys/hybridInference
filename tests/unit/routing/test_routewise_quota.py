"""Tests for RouteWise daily quota manager."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from routing.routewise.config import RouteWiseConfig
from routing.routewise.quota import QuotaManager


def _make_config(**overrides) -> RouteWiseConfig:
    """Create a RouteWiseConfig with optional overrides."""
    defaults = {
        "daily_quota": 1000,
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

    def test_used_fraction_progresses(self):
        """used_fraction = used / daily_quota, clamped to [0, 1]."""
        mgr = QuotaManager(_make_config(daily_quota=10))
        assert mgr.used_fraction == 0.0
        for _ in range(5):
            mgr.consume()
        assert mgr.used_fraction == pytest.approx(0.5)
        for _ in range(10):
            mgr.consume()
        assert mgr.used_fraction == 1.0

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
            assert mgr.remaining == 100

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
