"""Tests for RouteWise configuration loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from routing.routewise.config import RouteWiseConfig, load_routewise_config

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.unit
class TestRouteWiseConfigDefaults:
    """Verify all default values are sane."""

    def test_default_config(self):
        cfg = RouteWiseConfig()
        assert cfg.random_seed is None
        assert cfg.reference_api_price is None
        assert cfg.db_bootstrap_enabled is True
        assert cfg.db_bootstrap_max_rows == 50_000
        assert cfg.stateful_tiers_single_worker_only is True
        assert cfg.daily_quota == 5000
        assert cfg.quota_monthly_fee == 20.0
        assert cfg.reset_timezone == "UTC"
        assert cfg.quota_snapshot_refresh_interval_sec == 60.0
        assert cfg.concurrency_enabled is False
        assert cfg.concurrency_limit == 8
        assert cfg.concurrency_monthly_fee == 25.0
        assert cfg.shadow_price_window_hours == 24
        assert cfg.envelope_lower_percentile == 10.0
        assert cfg.envelope_upper_percentile == 90.0
        # Layer 2 defaults
        assert cfg.latency_slo_sec == 3.0
        assert cfg.latency_window_sec == 900.0
        assert cfg.latency_max_samples_per_profile == 5000
        assert cfg.latency_min_samples == 10
        assert cfg.latency_hedge_mode == "disabled"

    def test_invalid_latency_hedge_mode_rejected(self):
        with pytest.raises(ValueError, match="Unsupported latency_hedge_mode"):
            RouteWiseConfig(latency_hedge_mode="economic")


@pytest.mark.unit
class TestLoadFromYAML:
    """Verify YAML loading and merging."""

    def test_load_flat_yaml(self, tmp_path: Path):
        """Flat keys (backward compat) still load correctly."""
        yaml_content = (
            "routewise:\n"
            "  random_seed: 123\n"
            "  reference_api_price:\n"
            '    prompt: "1.2"\n'
            '    completion: "4.0"\n'
            "  daily_quota: 10000\n"
        )
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.random_seed == 123
        assert cfg.reference_api_price == {"prompt": "1.2", "completion": "4.0"}
        assert cfg.daily_quota == 10000
        # Defaults still apply for omitted keys
        assert cfg.concurrency_enabled is False

    def test_load_nested_db_bootstrap_yaml(self, tmp_path: Path):
        """Nested db_bootstrap section is flattened correctly."""
        yaml_content = "routewise:\n  db_bootstrap:\n    enabled: false\n    max_rows: 123\n"
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.db_bootstrap_enabled is False
        assert cfg.db_bootstrap_max_rows == 123

    def test_load_nested_adr_yaml(self, tmp_path: Path):
        """Nested ADR structure (quota/concurrency/envelope) is flattened."""
        yaml_content = (
            "routewise:\n"
            "  quota:\n"
            "    daily_quota: 8000\n"
            "    monthly_fee: 30.0\n"
            "    reset_timezone: US/Eastern\n"
            "    snapshot_refresh_interval_sec: 15.0\n"
            "  concurrency:\n"
            "    enabled: true\n"
            "    limit: 16\n"
            "    monthly_fee: 50.0\n"
            "  envelope:\n"
            "    window_hours: 48\n"
            "    lower_percentile: 5\n"
            "    upper_percentile: 95\n"
        )
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        # Quota section
        assert cfg.daily_quota == 8000
        assert cfg.quota_monthly_fee == 30.0
        assert cfg.reset_timezone == "US/Eastern"
        assert cfg.quota_snapshot_refresh_interval_sec == 15.0
        # Concurrency section
        assert cfg.concurrency_enabled is True
        assert cfg.concurrency_limit == 16
        assert cfg.concurrency_monthly_fee == 50.0
        # Envelope section
        assert cfg.shadow_price_window_hours == 48
        assert cfg.envelope_lower_percentile == 5
        assert cfg.envelope_upper_percentile == 95

    def test_load_missing_file_uses_defaults(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist.yaml"
        cfg = load_routewise_config(missing)
        assert cfg == RouteWiseConfig()

    def test_load_empty_yaml_uses_defaults(self, tmp_path: Path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cfg = load_routewise_config(p)
        assert cfg == RouteWiseConfig()

    def test_malformed_yaml_uses_defaults(self, tmp_path: Path, caplog):
        """Malformed YAML falls back to defaults with a warning."""
        p = tmp_path / "malformed.yaml"
        p.write_text("routewise:\n  quota: [1, 2\n")

        cfg = load_routewise_config(p)
        assert cfg == RouteWiseConfig()
        assert "Failed to load RouteWise config" in caplog.text

    def test_non_mapping_root_uses_defaults(self, tmp_path: Path, caplog):
        """A non-mapping YAML root falls back to defaults."""
        p = tmp_path / "list_root.yaml"
        p.write_text("- just\n- a\n- list\n")

        cfg = load_routewise_config(p)
        assert cfg == RouteWiseConfig()
        assert "must be a mapping at the root" in caplog.text

    def test_non_mapping_routewise_section_uses_defaults(self, tmp_path: Path, caplog):
        """A non-mapping `routewise` section falls back to defaults."""
        p = tmp_path / "bad_section.yaml"
        p.write_text("routewise:\n  - not\n  - a_map\n")

        cfg = load_routewise_config(p)
        assert cfg == RouteWiseConfig()
        assert "section 'routewise'" in caplog.text

    def test_unknown_top_level_key_warns(self, tmp_path: Path, caplog):
        """Unrecognized top-level keys emit a warning."""
        yaml_content = "routewise:\n  random_seed: 7\n  unknown_future_key: 42\n"
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.random_seed == 7
        assert not hasattr(cfg, "unknown_future_key")
        assert "unrecognized key 'unknown_future_key'" in caplog.text

    def test_unknown_nested_key_warns(self, tmp_path: Path, caplog):
        """Unrecognized keys inside nested sections emit a warning."""
        yaml_content = "routewise:\n  quota:\n    daily_quota: 5000\n    bogus_field: true\n"
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.daily_quota == 5000
        assert "unrecognized key 'quota.bogus_field'" in caplog.text

    def test_load_nested_latency_yaml(self, tmp_path: Path):
        """Nested latency section is flattened correctly."""
        yaml_content = (
            "routewise:\n"
            "  latency:\n"
            "    slo_sec: 5.0\n"
            "    window_sec: 600.0\n"
            "    max_samples: 123\n"
            "    min_samples: 20\n"
            "    hedge_mode: disabled\n"
        )
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.latency_slo_sec == 5.0
        assert cfg.latency_window_sec == 600.0
        assert cfg.latency_max_samples_per_profile == 123
        assert cfg.latency_min_samples == 20
        assert cfg.latency_hedge_mode == "disabled"

    def test_load_invalid_latency_hedge_mode_raises(self, tmp_path: Path):
        p = tmp_path / "routewise.yaml"
        p.write_text("routewise:\n  latency:\n    hedge_mode: shadow\n")

        with pytest.raises(ValueError, match="Unsupported latency_hedge_mode"):
            load_routewise_config(p)

    def test_load_flat_latency_keys(self, tmp_path: Path):
        """Flat latency_* keys also load correctly."""
        yaml_content = (
            "routewise:\n"
            "  latency_slo_sec: 2.0\n"
            "  latency_max_samples_per_profile: 123\n"
            "  latency_min_samples: 5\n"
        )
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.latency_slo_sec == 2.0
        assert cfg.latency_max_samples_per_profile == 123
        assert cfg.latency_min_samples == 5

    def test_load_canary_config_nested(self, tmp_path: Path):
        """Nested canary section is flattened correctly."""
        yaml_content = (
            "routewise:\n"
            "  canary:\n"
            "    enabled: true\n"
            "    enabled_models:\n"
            "      - model-a\n"
            "      - model-b\n"
            "    traffic_fraction: 0.25\n"
        )
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.canary_enabled is True
        assert cfg.canary_enabled_models == ["model-a", "model-b"]
        assert cfg.canary_traffic_fraction == 0.25

    def test_canary_defaults(self):
        """Canary fields have safe defaults when section absent."""
        cfg = RouteWiseConfig()
        assert cfg.canary_enabled is False
        assert cfg.canary_enabled_models is None
        assert cfg.canary_traffic_fraction == 1.0
