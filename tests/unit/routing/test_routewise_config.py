"""Tests for RouteWise configuration defaults and validation."""

from __future__ import annotations

import pytest

from routing.routewise.config import RouteWiseConfig


@pytest.mark.unit
class TestRouteWiseConfigDefaults:
    """Verify all default values are sane."""

    def test_default_config(self):
        cfg = RouteWiseConfig()
        assert cfg.random_seed is None
        assert cfg.reference_api_price is None
        assert cfg.db_bootstrap_enabled is True
        assert cfg.db_bootstrap_max_rows == 50_000
        assert cfg.stateful_providers_single_worker_only is True
        assert cfg.quota_snapshot_refresh_interval_sec == 60.0
        assert cfg.envelope_window_hours == 24
        assert cfg.envelope_lower_percentile == 10.0
        assert cfg.envelope_upper_percentile == 90.0
        # Latency-layer defaults
        assert cfg.latency_slo_sec == 3.0
        assert cfg.latency_window_sec == 900.0
        assert cfg.latency_max_samples_per_profile == 5000
        assert cfg.latency_min_samples == 10
        assert cfg.latency_hedge_mode == "disabled"

    def test_invalid_latency_hedge_mode_rejected(self):
        with pytest.raises(ValueError, match="Unsupported latency_hedge_mode"):
            RouteWiseConfig(latency_hedge_mode="economic")

    def test_canary_defaults(self):
        """Canary fields have safe defaults when section absent."""
        cfg = RouteWiseConfig()
        assert cfg.canary_enabled is False
        assert cfg.canary_enabled_models is None
        assert cfg.canary_traffic_fraction == 1.0

    def test_resource_fields_are_gone(self):
        """Resource limits are route-level config, not RouteWiseConfig fields."""
        cfg = RouteWiseConfig()
        for moved in (
            "daily_quota",
            "quota_monthly_fee",
            "reset_timezone",
            "concurrency_enabled",
            "concurrency_limit",
            "concurrency_monthly_fee",
        ):
            assert not hasattr(cfg, moved)
