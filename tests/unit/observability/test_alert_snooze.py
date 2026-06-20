"""Tests for serving.observability.alert_snooze and its alert_slack hook."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from serving.observability import alert_snooze
from serving.observability.alerts import AlertSeverity, alert_slack, reset_dedupe_state


class FakeStore:
    """Minimal in-memory site_settings store."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def get_setting(self, key: str):
        return self.rows.get(key)

    async def set_setting(self, key, value, value_type, updated_by):
        self.rows[key] = {"value": value, "value_type": value_type, "updated_by": updated_by}


@pytest.fixture
def store():
    s = FakeStore()
    alert_snooze.init_alert_snooze(s)
    yield s
    alert_snooze.init_alert_snooze(None)


@pytest.fixture(autouse=True)
def reset_state():
    reset_dedupe_state()
    yield
    reset_dedupe_state()


async def test_not_snoozed_when_store_uninitialized():
    alert_snooze.init_alert_snooze(None)
    assert await alert_snooze.is_snoozed() is False
    assert await alert_snooze.get_snooze_until() == 0.0


async def test_set_and_get_snooze(store):
    until = time.time() + 3600
    await alert_snooze.set_snooze_until(until, "admin@x.com")
    assert await alert_snooze.is_snoozed() is True
    assert abs(await alert_snooze.get_snooze_until() - until) < 1.0
    assert store.rows[alert_snooze.SNOOZE_SETTING_KEY]["value_type"] == "float"


async def test_past_deadline_is_not_snoozed(store):
    await alert_snooze.set_snooze_until(time.time() - 10, "admin@x.com")
    assert await alert_snooze.is_snoozed() is False


async def test_clear_snooze(store):
    await alert_snooze.set_snooze_until(time.time() + 3600, "admin@x.com")
    await alert_snooze.clear_snooze("admin@x.com")
    assert await alert_snooze.is_snoozed() is False


async def test_alert_slack_suppressed_when_snoozed(monkeypatch, store):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    await alert_snooze.set_snooze_until(time.time() + 3600, "admin@x.com")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(return_value=True),
    ) as mock_post:
        sent = await alert_slack(AlertSeverity.ERROR, "test", {"k": "v"})
    assert sent is False
    mock_post.assert_not_called()


async def test_alert_slack_sends_when_not_snoozed(monkeypatch, store):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    await alert_snooze.clear_snooze("admin@x.com")
    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(return_value=True),
    ) as mock_post:
        sent = await alert_slack(AlertSeverity.ERROR, "test", {"k": "v"})
    assert sent is True
    mock_post.assert_awaited_once()
