"""Tests for the alert config loader."""

import textwrap
from pathlib import Path

from serving.observability.alert_config import AlertConfig, load_alert_config


def test_load_alert_config_minimal(tmp_path: Path):
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            rules:
              failed_request_rate:
                enabled: true
                window_sec: 300
                threshold_pct: 5.0
                min_samples: 50
                cooldown_sec: 900
            state_changes:
              circuit_open:
                enabled: true
                cooldown_sec: 300
            cost:
              user_overrun:
                enabled: false
                check_interval_sec: 300
                cooldown_sec: 86400
                thresholds_per_role: {free: 5.0}
            """
        )
    )
    cfg = load_alert_config(p)
    assert isinstance(cfg, AlertConfig)
    assert cfg.rules.failed_request_rate.threshold_pct == 5.0
    assert cfg.state_changes.circuit_open.enabled is True
    assert cfg.cost.user_overrun.thresholds_per_role["free"] == 5.0


def test_load_alert_config_missing_file_returns_defaults(tmp_path: Path):
    cfg = load_alert_config(tmp_path / "missing.yaml")
    assert cfg.rules.failed_request_rate.enabled is True
    # All-default config should be valid.
