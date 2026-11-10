"""The admin global alert snooze: its endpoints, and what it does to the alert path.

``POST /admin/alerts/snooze`` silences every Slack alert until a deadline, and
``DELETE`` lifts it at once. None of the three endpoints had a test. What matters
operationally is the join: while snoozed a breach sends nothing, a resolution
still does, and once the snooze lapses or is cleared, breaches page again.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.observability import alert_mutes, alert_snooze
from serving.observability.alerts import AlertSeverity, alert_slack, reset_dedupe_state
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router

pytestmark = pytest.mark.asyncio

AUTH = {"Authorization": "Bearer test-admin"}


class FakeStore:
    """In-memory ``site_settings`` with the calls the snooze and mutes use."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def get_setting(self, key):
        row = self.rows.get(key)
        return None if row is None else {"key": key, **row}

    async def list_settings(self):
        return [{"key": key, **row} for key, row in sorted(self.rows.items())]

    async def set_setting(self, key, value, value_type, updated_by):
        self.rows[key] = {
            "value": value,
            "value_type": value_type,
            "updated_at": datetime.now(UTC),
            "updated_by": updated_by,
        }

    async def delete_setting(self, key):
        return self.rows.pop(key, None) is not None


@pytest.fixture
async def admin_client(monkeypatch):
    store = FakeStore()
    alert_snooze.init_alert_snooze(store)
    alert_mutes.init_alert_mutes(store)
    reset_dedupe_state()
    app = FastAPI()
    app.state.services = AppServices(
        router=MagicMock(),
        operational_store=MagicMock(),
        db_logger=MagicMock(),
        log_store=MagicMock(),
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    audit = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.alerts.log_admin_action", audit)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store, audit
    alert_snooze.init_alert_snooze(None)
    alert_mutes.init_alert_mutes(None)
    reset_dedupe_state()


async def _fire(key: str, *, status: str = "firing") -> bool:
    return await alert_slack(
        AlertSeverity.ERROR, "Provider circuit opened", {}, dedupe_key=key, status=status
    )


async def test_nothing_is_snoozed_at_first(admin_client):
    client, _, _ = admin_client

    response = await client.get("/admin/alerts/snooze", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"snoozed": False, "snooze_until": None, "seconds_remaining": 0}


async def test_a_snooze_is_stored_reported_and_audited(admin_client):
    client, store, audit = admin_client
    before = time.time()

    response = await client.post(
        "/admin/alerts/snooze", json={"duration_seconds": 600}, headers=AUTH
    )

    body = response.json()
    assert response.status_code == 200 and body["snoozed"] is True
    assert 590 <= body["seconds_remaining"] <= 600
    assert before + 600 <= body["snooze_until"] <= time.time() + 600
    assert store.rows  # persisted, so every worker and a restart see it
    assert (await client.get("/admin/alerts/snooze", headers=AUTH)).json()["snoozed"] is True
    assert audit.await_args.kwargs["action"] == "alerts.snooze"
    assert audit.await_args.kwargs["details"]["duration_seconds"] == 600


async def test_a_snoozed_breach_sends_nothing_and_a_cleared_one_pages_again(admin_client):
    client, store, audit = admin_client
    await client.post("/admin/alerts/snooze", json={"duration_seconds": 600}, headers=AUTH)

    with patch(
        "serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)
    ) as post:
        assert await _fire("circuit_open:zhipu") is False
        post.assert_not_awaited()

        cleared = await client.delete("/admin/alerts/snooze", headers=AUTH)
        assert cleared.json()["snoozed"] is False
        assert await _fire("circuit_open:zhipu") is True
        post.assert_awaited_once()

    # Cleared to a deadline in the past, so every worker reads it as lifted.
    assert float(store.rows["slack_alerts_snooze_until"]["value"]) == 0.0
    assert audit.await_args.kwargs["action"] == "alerts.snooze_clear"


async def test_a_lapsed_snooze_no_longer_silences(admin_client):
    client, _, _ = admin_client
    await alert_snooze.set_snooze_until(time.time() - 1, "admin")

    status = (await client.get("/admin/alerts/snooze", headers=AUTH)).json()
    with patch("serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)):
        sent = await _fire("circuit_open:moonshot")

    assert status == {"snoozed": False, "snooze_until": None, "seconds_remaining": 0}
    assert sent is True


async def test_a_resolution_is_never_snoozed(admin_client):
    """An incident that opened before the snooze must still be seen to close."""
    client, _, _ = admin_client
    with patch(
        "serving.observability.alerts._post_to_slack", new=AsyncMock(return_value=True)
    ) as post:
        assert await _fire("circuit_open:deepseek") is True
        await client.post("/admin/alerts/snooze", json={"duration_seconds": 600}, headers=AUTH)

        assert await _fire("circuit_open:deepseek", status="resolved") is True

    assert post.await_count == 2


@pytest.mark.parametrize("duration", [0, -5, 7 * 24 * 3600 + 1])
async def test_a_snooze_must_be_positive_and_at_most_a_week(admin_client, duration):
    client, store, audit = admin_client

    response = await client.post(
        "/admin/alerts/snooze", json={"duration_seconds": duration}, headers=AUTH
    )

    assert response.status_code == 422
    assert store.rows == {}
    audit.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "body"), [("GET", None), ("POST", {"duration_seconds": 60}), ("DELETE", None)]
)
async def test_only_an_admin_may_read_or_change_the_snooze(admin_client, method, body):
    client, store, _ = admin_client

    response = await client.request(method, "/admin/alerts/snooze", json=body)

    assert response.status_code in (401, 403)
    assert store.rows == {}
