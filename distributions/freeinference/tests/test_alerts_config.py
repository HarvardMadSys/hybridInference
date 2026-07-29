"""FreeInference's alert rules, checked where they now live.

Upstream ships no alert config: alerts.yaml is one deployment's thresholds
and inboxes. The assertion moved with the file, because an upstream test
reading a distribution's file is the coupling the overlay exists to
prevent — and because there is nothing at the old path to read.
"""

from __future__ import annotations


def test_alerts_yaml_loads_with_pending_prefix_cache_leak() -> None:
    """This deployment's alert config parses, and its leak block is deliberate."""
    from pathlib import Path

    import yaml

    from serving.observability.alert_config import AlertConfig

    # distributions/freeinference/tests/<file>.py -> the overlay root
    yaml_path = Path(__file__).resolve().parents[1] / "config" / "alerts.yaml"
    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    assert "prefix_cache_pending_leak" in data["rules"]
    cfg = AlertConfig.model_validate(data)
    leak = cfg.rules.prefix_cache_pending_leak
    assert leak.enabled is True
    assert leak.window_sec == 600
    assert leak.threshold_count == 20
    assert leak.cooldown_sec == 3600


def test_alerts_yaml_loads_with_tracked_task_failure_rate() -> None:
    """The committed alerts.yaml parses cleanly into AlertConfig with our defaults."""
    from pathlib import Path

    import yaml

    from serving.observability.alert_config import AlertConfig

    # tests/unit/observability -> repo root is parents[3].
    yaml_path = Path(__file__).resolve().parents[1] / "config" / "alerts.yaml"
    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    cfg = AlertConfig(**data)
    rule_cfg = cfg.rules.tracked_task_failure_rate
    assert rule_cfg.enabled is True
    assert rule_cfg.window_sec == 300
    assert rule_cfg.threshold_pct == 5.0
    assert rule_cfg.min_samples == 50
    assert rule_cfg.cooldown_sec == 1800
