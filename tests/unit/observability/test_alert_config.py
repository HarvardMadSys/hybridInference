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


def test_circuit_open_page_on_usage_limit_defaults_to_paging(tmp_path: Path):
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            state_changes:
              circuit_open:
                enabled: true
                cooldown_sec: 300
            """
        )
    )
    assert load_alert_config(p).state_changes.circuit_open.page_on_usage_limit is True


def test_circuit_open_page_on_usage_limit_can_be_turned_off(tmp_path: Path):
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            state_changes:
              circuit_open:
                enabled: true
                cooldown_sec: 300
                page_on_usage_limit: false
            """
        )
    )
    assert load_alert_config(p).state_changes.circuit_open.page_on_usage_limit is False


def test_auth_failure_spike_off_with_no_alerts_file(tmp_path: Path):
    assert load_alert_config(tmp_path / "missing.yaml").rules.auth_failure_spike.enabled is False


def test_auth_failure_spike_stays_off_when_only_thresholds_are_tuned(tmp_path: Path):
    """A partial rule block must not resurrect the page.

    Pydantic builds the nested model from whatever mapping the YAML supplies,
    so a field-level ``default_factory`` never runs here — the model's own
    ``enabled`` default is what keeps this off.
    """
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            rules:
              auth_failure_spike:
                window_sec: 120
                threshold_count: 100
            """
        )
    )
    cfg = load_alert_config(p)
    assert cfg.rules.auth_failure_spike.enabled is False
    # The tuning still lands — it is only the paging that stays off.
    assert cfg.rules.auth_failure_spike.window_sec == 120
    assert cfg.rules.auth_failure_spike.threshold_count == 100


def test_auth_failure_spike_can_be_turned_back_on(tmp_path: Path):
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            rules:
              auth_failure_spike:
                enabled: true
                threshold_count: 100
            """
        )
    )
    cfg = load_alert_config(p)
    assert cfg.rules.auth_failure_spike.enabled is True
    assert cfg.rules.auth_failure_spike.threshold_count == 100


def test_auth_ip_blocked_on_with_no_alerts_file(tmp_path: Path):
    """A gateway blocking a source pages out of the box.

    The mirror of ``test_auth_failure_spike_off_with_no_alerts_file``: the
    failures are noise, the block they produce is a decision.
    """
    cfg = load_alert_config(tmp_path / "missing.yaml")
    assert cfg.rules.auth_ip_blocked.enabled is True
    # One block is a breach; see AuthIpBlockedConfig.
    assert cfg.rules.auth_ip_blocked.threshold_count == 1
    assert cfg.rules.auth_ip_blocked.window_sec == 300


def test_auth_ip_blocked_stays_on_when_only_thresholds_are_tuned(tmp_path: Path):
    """A partial rule block must not silently turn the page off."""
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            rules:
              auth_ip_blocked:
                window_sec: 600
                threshold_count: 3
            """
        )
    )
    cfg = load_alert_config(p)
    assert cfg.rules.auth_ip_blocked.enabled is True
    assert cfg.rules.auth_ip_blocked.window_sec == 600
    assert cfg.rules.auth_ip_blocked.threshold_count == 3


def test_auth_ip_blocked_can_be_turned_off(tmp_path: Path):
    """A deployment that wants only the log record opts out explicitly."""
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            rules:
              auth_ip_blocked:
                enabled: false
            """
        )
    )
    assert load_alert_config(p).rules.auth_ip_blocked.enabled is False


def test_client_error_burst_on_with_no_alerts_file(tmp_path: Path):
    """Deliberate default: a relayed-4xx storm pages out of the box.

    Unlike ``auth_failure_spike`` — anonymous scanner noise with nothing to act
    on — every event this counts is an authenticated, routed request that failed
    in front of a user, and the card names the endpoint and status. The alerting
    framework as a whole is still off unless a deployment sets ``ALERTS_ENABLED``
    / ``SLACK_ALERTS_WEBHOOK_URL``, so "on" here only affects a deployment that
    has already asked to be paged.
    """
    cfg = load_alert_config(tmp_path / "missing.yaml")
    assert cfg.rules.client_error_burst.enabled is True
    assert cfg.rules.client_error_burst.window_sec == 600
    assert cfg.rules.client_error_burst.threshold_count == 12
    assert cfg.rules.client_error_burst.cooldown_sec == 3600


def test_stream_failure_rate_on_with_no_alerts_file(tmp_path: Path):
    """Same deliberate default, and a **count** despite the ``_rate`` suffix.

    The ``Stream failed for model=`` log line carries no denominator — nothing
    counts the streams that finished — so a percentage would be measured against
    a number the rule cannot see.
    """
    cfg = load_alert_config(tmp_path / "missing.yaml")
    assert cfg.rules.stream_failure_rate.enabled is True
    assert cfg.rules.stream_failure_rate.window_sec == 600
    assert cfg.rules.stream_failure_rate.threshold_count == 8
    assert cfg.rules.stream_failure_rate.cooldown_sec == 3600


def test_overlay_alerts_yaml_block_parses_exactly(tmp_path: Path):
    """The block the overlay's alerts.yaml ships, verbatim.

    ``AlertConfig`` ignores unknown keys silently, so a field renamed here is
    not a boot failure — it is the overlay's tuning being dropped and the rule
    running on defaults nobody chose, with nothing to say so.
    """
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            rules:
              client_error_burst:
                enabled: true
                window_sec: 600
                threshold_count: 12
                cooldown_sec: 3600
              stream_failure_rate:
                enabled: true
                window_sec: 600
                threshold_count: 8
                cooldown_sec: 3600
            """
        )
    )
    rules = load_alert_config(p).rules
    assert rules.client_error_burst.model_dump() == {
        "enabled": True,
        "window_sec": 600,
        "threshold_count": 12,
        "cooldown_sec": 3600,
    }
    assert rules.stream_failure_rate.model_dump() == {
        "enabled": True,
        "window_sec": 600,
        "threshold_count": 8,
        "cooldown_sec": 3600,
    }


def test_new_rules_stay_on_when_only_thresholds_are_tuned(tmp_path: Path):
    """A partial rule block must not silently turn either page off."""
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            rules:
              client_error_burst:
                threshold_count: 30
              stream_failure_rate:
                window_sec: 120
            """
        )
    )
    cfg = load_alert_config(p)
    assert cfg.rules.client_error_burst.enabled is True
    assert cfg.rules.client_error_burst.threshold_count == 30
    assert cfg.rules.stream_failure_rate.enabled is True
    assert cfg.rules.stream_failure_rate.window_sec == 120


def test_new_rules_can_be_turned_off(tmp_path: Path):
    """A deployment that wants only the log records opts out explicitly."""
    p = tmp_path / "alerts.yaml"
    p.write_text(
        textwrap.dedent(
            """
            rules:
              client_error_burst:
                enabled: false
              stream_failure_rate:
                enabled: false
            """
        )
    )
    cfg = load_alert_config(p)
    assert cfg.rules.client_error_burst.enabled is False
    assert cfg.rules.stream_failure_rate.enabled is False
