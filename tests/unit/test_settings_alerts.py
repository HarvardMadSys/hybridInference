"""Tests for the new alerting framework settings."""

from serving.config.settings import get_settings


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
