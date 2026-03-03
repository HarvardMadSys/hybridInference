"""Tests for RouteWise configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from routing.routewise.config import RouteWiseConfig, load_routewise_config


@pytest.mark.unit
class TestRouteWiseConfigDefaults:
    """Verify all default values are sane."""

    def test_default_config(self):
        cfg = RouteWiseConfig()
        assert cfg.decision_rule == "pd"
        assert cfg.predictor == "ema"
        assert cfg.risk_quantile == 0.10
        assert cfg.daily_quota == 5000
        assert cfg.quota_monthly_fee == 20.0
        assert cfg.reset_timezone == "UTC"
        assert cfg.concurrency_enabled is False
        assert cfg.concurrency_limit == 8
        assert cfg.concurrency_monthly_fee == 25.0
        assert cfg.shadow_price_L_seed == 0.001
        assert cfg.shadow_price_U_seed == 0.500
        assert cfg.shadow_price_adaptive is True
        assert cfg.shadow_price_window_hours == 24
        assert cfg.shadow_price_min_ratio == 10


@pytest.mark.unit
class TestLoadFromYAML:
    """Verify YAML loading and merging."""

    def test_load_flat_yaml(self, tmp_path: Path):
        """Flat keys (backward compat) still load correctly."""
        yaml_content = (
            "routewise:\n"
            "  decision_rule: lapd\n"
            "  daily_quota: 10000\n"
            "  risk_quantile: 0.05\n"
            "  shadow_price_adaptive: false\n"
        )
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.decision_rule == "lapd"
        assert cfg.daily_quota == 10000
        assert cfg.risk_quantile == 0.05
        assert cfg.shadow_price_adaptive is False
        # Defaults still apply for omitted keys
        assert cfg.predictor == "ema"
        assert cfg.concurrency_enabled is False

    def test_load_nested_adr_yaml(self, tmp_path: Path):
        """Nested ADR structure (quota/concurrency/shadow_price) is flattened."""
        yaml_content = (
            "routewise:\n"
            "  decision_rule: lapd\n"
            "  predictor: histogram\n"
            "  quota:\n"
            "    daily_quota: 8000\n"
            "    monthly_fee: 30.0\n"
            "    reset_timezone: US/Eastern\n"
            "  concurrency:\n"
            "    enabled: true\n"
            "    limit: 16\n"
            "    monthly_fee: 50.0\n"
            "  shadow_price:\n"
            "    L_seed: 0.01\n"
            "    U_seed: 1.0\n"
            "    adaptive: false\n"
            "    window_hours: 48\n"
            "    min_ratio: 20\n"
        )
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.decision_rule == "lapd"
        assert cfg.predictor == "histogram"
        # Quota section
        assert cfg.daily_quota == 8000
        assert cfg.quota_monthly_fee == 30.0
        assert cfg.reset_timezone == "US/Eastern"
        # Concurrency section
        assert cfg.concurrency_enabled is True
        assert cfg.concurrency_limit == 16
        assert cfg.concurrency_monthly_fee == 50.0
        # Shadow price section
        assert cfg.shadow_price_L_seed == 0.01
        assert cfg.shadow_price_U_seed == 1.0
        assert cfg.shadow_price_adaptive is False
        assert cfg.shadow_price_window_hours == 48
        assert cfg.shadow_price_min_ratio == 20

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

    def test_non_mapping_routewise_section_uses_defaults(
        self, tmp_path: Path, caplog
    ):
        """A non-mapping `routewise` section falls back to defaults."""
        p = tmp_path / "bad_section.yaml"
        p.write_text("routewise:\n  - not\n  - a_map\n")

        cfg = load_routewise_config(p)
        assert cfg == RouteWiseConfig()
        assert "section 'routewise'" in caplog.text

    def test_unknown_top_level_key_warns(self, tmp_path: Path, caplog):
        """Unrecognized top-level keys emit a warning."""
        yaml_content = (
            "routewise:\n"
            "  decision_rule: pd\n"
            "  unknown_future_key: 42\n"
        )
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.decision_rule == "pd"
        assert not hasattr(cfg, "unknown_future_key")
        assert "unrecognized key 'unknown_future_key'" in caplog.text

    def test_unknown_nested_key_warns(self, tmp_path: Path, caplog):
        """Unrecognized keys inside nested sections emit a warning."""
        yaml_content = (
            "routewise:\n"
            "  quota:\n"
            "    daily_quota: 5000\n"
            "    bogus_field: true\n"
        )
        p = tmp_path / "routewise.yaml"
        p.write_text(yaml_content)

        cfg = load_routewise_config(p)
        assert cfg.daily_quota == 5000
        assert "unrecognized key 'quota.bogus_field'" in caplog.text
