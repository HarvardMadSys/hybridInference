"""Tests for the admin per-type alert mute endpoints.

The endpoints back the dashboard's "Alert types" list: every catalogued type,
whether it is muted, and a mute or unmute per type. What a mute then does to the
alert path is covered in ``tests/unit/observability/test_alert_mutes.py``; this
file checks the surface and the one join between them.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.observability import alert_mutes
from serving.observability.alert_types import ALERT_TYPES
from serving.observability.alerts import AlertSeverity, alert_slack, reset_dedupe_state
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router

AUTH = {"Authorization": "Bearer test-admin"}


class FakeStore:
    """In-memory ``site_settings`` with the three calls the mutes use."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def list_settings(self):
        return [{"key": key, **row} for key, row in sorted(self.rows.items())]

    async def set_setting(self, key, value, value_type, updated_by):
        self.rows[key] = {
            "value": value,
            "value_type": value_type,
            "updated_at": datetime.now(timezone.utc),
            "updated_by": updated_by,
        }

    async def delete_setting(self, key):
        return self.rows.pop(key, None) is not None


@pytest.fixture
async def admin_client(monkeypatch):
    store = FakeStore()
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
    audit = AsyncMock()
    monkeypatch.setattr("serving.servers.routers.admin.alerts.log_admin_action", audit)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store, audit

    alert_mutes.init_alert_mutes(None)
    reset_dedupe_state()


def _by_type(body: dict) -> dict[str, dict]:
    return {item["alert_type"]: item for item in body["types"]}


@pytest.mark.asyncio
async def test_lists_every_catalogued_type_and_none_is_muted(admin_client):
    client, _, _ = admin_client

    response = await client.get("/admin/alerts/mutes", headers=AUTH)

    assert response.status_code == 200
    types = response.json()["types"]
    assert [item["alert_type"] for item in types] == [t.id for t in ALERT_TYPES]
    blocked = _by_type(response.json())["auth_ip_blocked"]
    assert blocked["label"] == "Auth-failure blocklist refusing a source"
    assert blocked["group"] == "Auth"
    assert blocked["key_pattern"] == "auth_ip_blocked"
    assert all(item["muted"] is False and item["muted_until"] is None for item in types)


@pytest.mark.asyncio
async def test_mute_for_a_duration_is_listed_and_audited(admin_client):
    client, store, audit = admin_client
    before = time.time()

    response = await client.put(
        "/admin/alerts/mutes/auth_ip_blocked",
        json={"duration_seconds": 3600},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["alert_type"] == "auth_ip_blocked"
    assert body["muted"] is True
    assert before + 3600 <= body["muted_until"] <= time.time() + 3600
    assert body["muted_by"] == store.rows["slack_alert_mute:auth_ip_blocked"]["updated_by"]
    audit.assert_awaited_once()
    assert audit.await_args.args[2] == "alerts.mute"
    assert audit.await_args.args[4]["alert_type"] == "auth_ip_blocked"
    assert audit.await_args.args[4]["duration_seconds"] == 3600

    listed = _by_type((await client.get("/admin/alerts/mutes", headers=AUTH)).json())
    assert listed["auth_ip_blocked"]["muted"] is True
    assert listed["auth_ip_blocked"]["muted_until"] == body["muted_until"]
    # Muting one type leaves every other type sending.
    assert not any(item["muted"] for key, item in listed.items() if key != "auth_ip_blocked")


@pytest.mark.parametrize("payload", [{"duration_seconds": None}, {}])
@pytest.mark.asyncio
async def test_mute_without_a_duration_lasts_until_unmuted(admin_client, payload):
    client, _, _ = admin_client

    response = await client.put("/admin/alerts/mutes/circuit_open", json=payload, headers=AUTH)

    assert response.status_code == 200
    assert response.json()["muted"] is True
    assert response.json()["muted_until"] is None
    listed = _by_type((await client.get("/admin/alerts/mutes", headers=AUTH)).json())
    assert listed["circuit_open"]["muted"] is True


@pytest.mark.asyncio
async def test_unmute_lifts_the_mute_and_is_audited(admin_client):
    client, store, audit = admin_client
    await client.put("/admin/alerts/mutes/circuit_open", json={}, headers=AUTH)
    audit.reset_mock()

    response = await client.delete("/admin/alerts/mutes/circuit_open", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["muted"] is False
    assert "slack_alert_mute:circuit_open" not in store.rows
    assert audit.await_args.args[2] == "alerts.unmute"
    assert audit.await_args.args[4] == {"alert_type": "circuit_open", "removed": True}
    listed = _by_type((await client.get("/admin/alerts/mutes", headers=AUTH)).json())
    assert listed["circuit_open"]["muted"] is False

    # Unmuting a type that is not muted is a harmless no-op.
    again = await client.delete("/admin/alerts/mutes/circuit_open", headers=AUTH)
    assert again.status_code == 200
    assert audit.await_args.args[4] == {"alert_type": "circuit_open", "removed": False}


@pytest.mark.asyncio
async def test_a_lapsed_mute_is_listed_as_not_muted(admin_client):
    client, _, _ = admin_client
    await alert_mutes.mute_alert_type("fivexx_rate", time.time() - 1, "admin@x.com")

    listed = _by_type((await client.get("/admin/alerts/mutes", headers=AUTH)).json())

    assert listed["fivexx_rate"]["muted"] is False
    assert listed["fivexx_rate"]["muted_until"] is None
    assert listed["fivexx_rate"]["muted_by"] is None


@pytest.mark.parametrize("method", ["put", "delete"])
@pytest.mark.asyncio
async def test_an_unknown_type_is_a_404_and_stores_nothing(admin_client, method):
    client, store, audit = admin_client
    kwargs = {"json": {}} if method == "put" else {}

    response = await getattr(client, method)(
        "/admin/alerts/mutes/not_an_alert", headers=AUTH, **kwargs
    )

    assert response.status_code == 404
    assert store.rows == {}
    audit.assert_not_awaited()


@pytest.mark.parametrize("duration", [0, -60, 30 * 24 * 60 * 60 + 1])
@pytest.mark.asyncio
async def test_an_out_of_range_duration_is_rejected(admin_client, duration):
    client, store, _ = admin_client

    response = await client.put(
        "/admin/alerts/mutes/auth_ip_blocked",
        json={"duration_seconds": duration},
        headers=AUTH,
    )

    assert response.status_code == 422
    assert store.rows == {}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin/alerts/mutes"),
        ("put", "/admin/alerts/mutes/auth_ip_blocked"),
        ("delete", "/admin/alerts/mutes/auth_ip_blocked"),
    ],
)
@pytest.mark.asyncio
async def test_every_route_requires_admin(admin_client, method, path):
    client, store, _ = admin_client
    kwargs = {"json": {}} if method == "put" else {}

    response = await getattr(client, method)(
        path, headers={"Authorization": "Bearer wrong"}, **kwargs
    )

    assert response.status_code in (401, 403)
    assert store.rows == {}


@pytest.mark.asyncio
async def test_a_mute_set_here_silences_that_type_on_the_alert_path(admin_client, monkeypatch):
    client, _, _ = admin_client
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", "https://hooks.slack.com/x")
    await client.put("/admin/alerts/mutes/auth_ip_blocked", json={}, headers=AUTH)

    with patch(
        "serving.observability.alerts._post_to_slack",
        new=AsyncMock(return_value=True),
    ) as mock_post:
        muted = await alert_slack(
            AlertSeverity.WARN,
            "Auth-failure blocklist refusing a source",
            {},
            dedupe_key="auth_ip_blocked",
        )
        sent = await alert_slack(
            AlertSeverity.ERROR, "Provider circuit opened", {}, dedupe_key="circuit_open:zhipu"
        )

    assert muted is False
    assert sent is True
    mock_post.assert_awaited_once()
