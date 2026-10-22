"""Tests for the new alerting framework settings."""

import pytest

from serving.config.settings import Settings, get_settings


def test_alerts_enabled_default_false(monkeypatch):
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.alerts_enabled is False


def test_slack_alerts_webhook_url_falls_back_to_slack_webhook_url(monkeypatch):
    monkeypatch.delenv("SLACK_ALERTS_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/XYZ")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.slack_alerts_webhook_url == "https://hooks.slack.com/services/XYZ"


def test_alerts_config_path_default(monkeypatch):
    monkeypatch.delenv("ALERTS_CONFIG_PATH", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.alerts_config_path == "config/alerts.yaml"


def test_wecom_alerts_webhook_url_default(monkeypatch):
    monkeypatch.delenv("WECOM_ALERTS_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.wecom_alerts_webhook_url == ""


def test_wecom_alerts_webhook_url_from_env(monkeypatch):
    monkeypatch.setenv(
        "WECOM_ALERTS_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
    )
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.wecom_alerts_webhook_url.endswith("?key=test")


def test_a_whitespace_only_webhook_is_not_a_configured_channel(monkeypatch):
    """The senders strip before deciding a channel is set; the rule must too."""
    for variable in ("SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "WECOM_ALERTS_WEBHOOK_URL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "   ")
    monkeypatch.setenv("WECOM_ALERTS_WEBHOOK_URL", "   ")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.broken_rules() == {}


@pytest.mark.parametrize(
    "slack_env",
    ["SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL"],
)
def test_slack_and_wecom_webhooks_are_mutually_exclusive(monkeypatch, slack_env):
    """One deployment selects one channel; configuring both is a config error."""
    for variable in ("SLACK_ALERTS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "WECOM_ALERTS_WEBHOOK_URL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv(slack_env, "https://hooks.slack.test/x")
    monkeypatch.setenv(
        "WECOM_ALERTS_WEBHOOK_URL",
        "https://qyapi.weixin.test/cgi-bin/webhook/send?key=abc",
    )
    get_settings.cache_clear()

    # get_settings() defers rules so the database can replace either side;
    # startup judges the rule through broken_rules().
    settings = get_settings()
    rules = {
        message: fields
        for message, fields in settings.broken_rules().items()
        if "mutually exclusive" in message
    }
    assert len(rules) == 1
    message, fields = next(iter(rules.items()))
    assert slack_env in message
    assert "WECOM_ALERTS_WEBHOOK_URL" in message
    assert set(fields) == {
        "slack_alerts_webhook_url",
        "slack_webhook_url",
        "wecom_alerts_webhook_url",
    }

    # A direct build outside get_settings() applies the rule immediately.
    with pytest.raises(ValueError, match="mutually exclusive"):
        Settings()
