"""Conditional configuration writes and the live configuration status endpoint."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config import app_config, live, live_control
from serving.config.app_config_registry import ALERTS_RUNTIME_GROUP
from serving.config.settings import get_settings
from serving.observability import alert_runtime
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import config as config_routes
from tests.fixtures.app_config_store import FakeAppConfigStore

AUTH = {"Authorization": "Bearer test-admin"}
WEBHOOK = "https://hooks.slack.test/services/T9/B9/fictional-live-secret"


@pytest.fixture
async def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    for key in alert_runtime.KEYS:
        monkeypatch.delenv(key, raising=False)
    models = tmp_path / "models.yaml"
    models.write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(models))
    get_settings.cache_clear()
    store = FakeAppConfigStore()
    await app_config.load(store)
    app_config.attach(store)
    live.COORDINATOR.install_baselines()
    audit = AsyncMock()
    monkeypatch.setattr(config_routes, "log_admin_action", audit)
    app = FastAPI()
    app.state.services = AppServices(router=MagicMock(), operational_store=MagicMock())
    app.include_router(admin_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, store, audit


@pytest.mark.asyncio
async def test_get_names_the_saved_version(client) -> None:
    http, store, _ = client
    response = await http.get("/admin/config", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["source_version"] == {"epoch": store.epoch, "revision": store.revision}
    assert response.headers["ETag"] == f'"{store.epoch}:{store.revision}"'
    entries = {item["key"]: item for item in body["entries"]}
    assert entries["SLACK_WEBHOOK_URL"]["group"] == ALERTS_RUNTIME_GROUP
    assert entries["SLACK_WEBHOOK_URL"]["apply_mode"] == "restart"
    assert {group["name"] for group in body["groups"]} == {ALERTS_RUNTIME_GROUP}


@pytest.mark.asyncio
async def test_a_matching_if_match_saves_and_returns_the_committed_version(client) -> None:
    http, store, _ = client
    etag = (await http.get("/admin/config", headers=AUTH)).headers["ETag"]
    response = await http.patch(
        "/admin/config",
        headers={**AUTH, "If-Match": etag},
        json={"values": {"SITE_NAME": "Example"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved_version"]["revision"] == store.revision
    assert response.headers["ETag"] == f'"{store.epoch}:{store.revision}"'
    assert response.headers["ETag"] != etag


@pytest.mark.asyncio
async def test_a_stale_if_match_is_refused_and_stores_nothing(client) -> None:
    http, store, audit = client
    stale = (await http.get("/admin/config", headers=AUTH)).headers["ETag"]
    # Another administrator saves first.
    assert (
        await http.patch("/admin/config", headers=AUTH, json={"values": {"SITE_NAME": "First"}})
    ).status_code == 200
    audit.reset_mock()
    response = await http.patch(
        "/admin/config",
        headers={**AUTH, "If-Match": stale},
        json={"values": {"SITE_NAME": "Second", "SLACK_WEBHOOK_URL": WEBHOOK}},
    )
    assert response.status_code == 409
    assert "changed since it was loaded" in response.json()["detail"]
    assert response.headers["ETag"] == f'"{store.epoch}:{store.revision}"'
    assert store.rows["SITE_NAME"].value == "First"
    assert "SLACK_WEBHOOK_URL" not in store.rows
    audit.assert_not_awaited()
    assert WEBHOOK not in response.text
    reset = await http.delete("/admin/config/SITE_NAME", headers={**AUTH, "If-Match": stale})
    assert reset.status_code == 409
    assert store.rows["SITE_NAME"].value == "First"


@pytest.mark.asyncio
async def test_an_older_client_without_if_match_keeps_last_write_wins(client) -> None:
    http, store, _ = client
    for value in ("One", "Two"):
        response = await http.patch(
            "/admin/config", headers=AUTH, json={"values": {"SITE_NAME": value}}
        )
        assert response.status_code == 200
    assert store.rows["SITE_NAME"].value == "Two"


@pytest.mark.asyncio
async def test_a_malformed_if_match_is_a_bad_request(client) -> None:
    http, _, _ = client
    response = await http.patch(
        "/admin/config",
        headers={**AUTH, "If-Match": 'W/"weak"'},
        json={"values": {"SITE_NAME": "x"}},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_live_status_without_a_database(client, monkeypatch) -> None:
    http, _, _ = client
    monkeypatch.setattr(app_config, "attached_store", lambda: None)
    response = await http.get("/admin/config/live", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["database"] is False


@pytest.mark.asyncio
async def test_live_status_never_carries_a_secret(client, monkeypatch) -> None:
    http, store, audit = client
    store.set_contract(ALERTS_RUNTIME_GROUP, active=True)
    saved = await http.patch(
        "/admin/config", headers=AUTH, json={"values": {"SLACK_WEBHOOK_URL": WEBHOOK}}
    )
    assert saved.status_code == 200
    await alert_runtime.GROUP.run_once()
    assert alert_runtime.current().slack_webhook_url == WEBHOOK
    report = alert_runtime.GROUP.status.as_report()

    async def fetch_status(_store, *, this_instance="p-test"):
        return {
            "source": "app_config",
            "version": store.version.as_dict(),
            "this_instance": this_instance,
            "stale_after_seconds": 60.0,
            "groups": [
                {
                    "name": ALERTS_RUNTIME_GROUP,
                    "keys": list(alert_runtime.KEYS),
                    "schema_version": 1,
                    "contract": {"active": True, "schema_version": 1},
                    "coverage": {"registered": 1, "converged": 1, "verified": False},
                    "instances": [
                        {
                            "instance_id": this_instance,
                            "this_process": True,
                            "build": "unknown",
                            "host": "h",
                            "lifecycle": "running",
                            "started_at": None,
                            "heartbeat_age_seconds": 1.0,
                            "stale": False,
                            "supports": True,
                            "status": report,
                        }
                    ],
                }
            ],
            "unknown_groups": [],
        }

    monkeypatch.setattr(live_control, "fetch_status", fetch_status)
    response = await http.get("/admin/config/live", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    group = body["groups"][0]
    assert group["local"]["state"] == "applied"
    keys = {item["key"]: item for item in group["preview"]["keys"]}
    assert keys["SLACK_WEBHOOK_URL"] == {
        "key": "SLACK_WEBHOOK_URL",
        "secret": True,
        "saved": None,
        "is_set": True,
        "source": "database",
    }
    everything = json.dumps(
        [body, saved.json(), [call.kwargs for call in audit.await_args_list]], default=str
    )
    assert WEBHOOK not in everything
    assert "fictional-live-secret" not in everything


@pytest.mark.asyncio
async def test_a_cross_origin_console_can_read_the_conflict_etag(monkeypatch) -> None:
    """A console on another origin must see the ETag a version conflict answers with."""
    from serving.servers import app as app_module
    from serving.servers.middleware.cors import SettingsCORSMiddleware

    origin = "http://localhost:3001"
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", origin)
    get_settings.cache_clear()
    app = FastAPI()
    app.add_middleware(SettingsCORSMiddleware, **app_module.CORS_OPTIONS)

    @app.patch("/admin/config")
    async def conflict() -> None:
        from fastapi import HTTPException

        raise HTTPException(409, "changed", headers={"ETag": '"e1:7"'})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preflight = await client.options(
            "/admin/config",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "if-match, content-type",
            },
        )
        assert preflight.status_code == 200
        assert "if-match" in preflight.headers["access-control-allow-headers"].lower()
        refused = await client.patch(
            "/admin/config", headers={"Origin": origin, "If-Match": '"e1:6"'}
        )
    assert refused.status_code == 409
    assert refused.headers["etag"] == '"e1:7"'
    exposed = refused.headers["access-control-expose-headers"].lower().split(", ")
    assert "etag" in exposed
