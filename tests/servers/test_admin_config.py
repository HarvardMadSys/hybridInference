"""Admin endpoints for the database-backed configuration and the backend restart."""

from __future__ import annotations

import os
import signal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config import app_config
from serving.config.settings import get_settings
from serving.servers import restart
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import config as config_routes, system as system_routes
from tests.fixtures.app_config_store import FakeAppConfigStore, row

AUTH = {"Authorization": "Bearer test-admin"}


@pytest.fixture
async def config_client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("SMTP_PASSWORD", "env-smtp-password")
    monkeypatch.delenv("SITE_NAME", raising=False)
    models = tmp_path / "models.yaml"
    models.write_text(
        "models:\n"
        "  - id: chat\n"
        "    base_url: https://api.example/v1\n"
        "    api_key: ${CHAT_UPSTREAM_KEY}\n"
    )
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(models))
    monkeypatch.delenv("CHAT_UPSTREAM_KEY", raising=False)
    get_settings.cache_clear()

    store = FakeAppConfigStore()
    await app_config.load(store)
    app_config.attach(store)

    audit = AsyncMock()
    monkeypatch.setattr(config_routes, "log_admin_action", audit)
    monkeypatch.setattr(system_routes, "log_admin_action", audit)

    app = FastAPI()
    app.state.services = AppServices(router=MagicMock(), operational_store=MagicMock())
    app.include_router(admin_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store, audit


def _entries(body: dict) -> dict[str, dict]:
    return {entry["key"]: entry for entry in body["entries"]}


@pytest.mark.asyncio
async def test_get_requires_an_administrator(config_client) -> None:
    client, _store, _audit = config_client

    response = await client.get("/admin/config")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_lists_every_entry_without_secret_values(config_client, monkeypatch) -> None:
    client, store, _audit = config_client
    monkeypatch.setattr(restart, "restart_supported", lambda: True)

    response = await client.get("/admin/config", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"categories", "entries", "missing", "pending_restart", "restart_supported"}
    assert body["restart_supported"] is True
    assert {category["id"] for category in body["categories"]} >= {"email", "security"}
    entries = _entries(body)
    smtp = entries["SMTP_PASSWORD"]
    assert smtp["secret"] is True
    assert smtp["value"] is None
    assert smtp["default"] is None
    assert smtp["is_set"] is True
    assert smtp["source"] == "database"  # imported from the environment at boot
    assert set(smtp) == {
        "key",
        "category",
        "description",
        "type",
        "secret",
        "required",
        "missing",
        "is_set",
        "value",
        "default",
        "source",
        "restart_required",
        "pending_restart",
        "environment_ignored",
        "immutable",
        "setup",
        "custom",
        "invalid",
        "used_by",
        "updated_at",
        "updated_by",
    }
    secrets = [
        "env-smtp-password",
        *(stored.value for stored in store.rows.values() if stored.secret),
    ]
    assert {"JWT_SECRET_KEY", "API_KEY_SECRET", "SMTP_PASSWORD"} <= {
        key for key, stored in store.rows.items() if stored.secret
    }
    for secret in secrets:
        assert secret not in response.text


@pytest.mark.asyncio
async def test_get_reports_a_variable_a_model_needs(config_client) -> None:
    client, _store, _audit = config_client

    body = (await client.get("/admin/config", headers=AUTH)).json()

    entry = _entries(body)["CHAT_UPSTREAM_KEY"]
    assert entry["missing"] is True and entry["required"] is True
    assert entry["used_by"] == ["chat"]
    assert entry["secret"] is True
    assert "CHAT_UPSTREAM_KEY" in body["missing"]


@pytest.mark.asyncio
async def test_patch_stores_and_returns_the_full_configuration(config_client) -> None:
    client, store, audit = config_client

    response = await client.patch(
        "/admin/config",
        json={
            "values": {"SITE_NAME": "Acme Gateway", "SMTP_PORT": 2525, "CHAT_UPSTREAM_KEY": "k"},
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    entries = _entries(response.json())
    assert entries["SITE_NAME"]["value"] == "Acme Gateway"
    assert entries["SMTP_PORT"]["value"] == 2525
    assert get_settings().smtp_port == 2525
    # Set, but the model registry read it at startup.
    assert entries["CHAT_UPSTREAM_KEY"]["missing"] is False
    assert entries["CHAT_UPSTREAM_KEY"]["pending_restart"] is True
    assert "CHAT_UPSTREAM_KEY" in response.json()["pending_restart"]
    assert store.writes and {stored.key for stored in store.writes[-1]} == {
        "SITE_NAME",
        "SMTP_PORT",
        "CHAT_UPSTREAM_KEY",
    }
    details = [call.args[4] for call in audit.await_args_list]
    assert {"key": "CHAT_UPSTREAM_KEY", "changed": True} in details
    assert {"key": "SITE_NAME", "old_value": None, "new_value": "Acme Gateway"} in details
    assert all(call.args[2] == "config.update" for call in audit.await_args_list)
    assert '"k"' not in repr(details)


@pytest.mark.asyncio
async def test_patch_marks_new_custom_variables_secret_on_request(config_client) -> None:
    client, store, _audit = config_client

    response = await client.patch(
        "/admin/config",
        json={"values": {"BOX_TOKEN": "s3cret"}, "secrets": {"BOX_TOKEN": True}},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert store.rows["BOX_TOKEN"].secret
    entry = _entries(response.json())["BOX_TOKEN"]
    assert entry["custom"] is True and entry["value"] is None
    assert "s3cret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values,status,detail",
    [
        ({"SMTP_PORT": "x"}, 400, "SMTP_PORT: must be an integer."),
        (
            {"TRUST_CLOUDFLARE_HEADERS": True},
            400,
            "TRUST_CLOUDFLARE_HEADERS: TRUST_CLOUDFLARE_HEADERS requires TRUST_PROXY_HEADERS",
        ),
        ({"DB_HOST": "elsewhere"}, 403, None),
        ({"API_KEY_SECRET": "new"}, 409, "API_KEY_SECRET is already set and cannot be changed."),
    ],
)
async def test_patch_refusals_store_nothing(config_client, values, status, detail) -> None:
    client, store, audit = config_client
    writes = len(store.writes)

    response = await client.patch(
        "/admin/config", json={"values": {"SITE_NAME": "ok", **values}}, headers=AUTH
    )

    assert response.status_code == status, response.text
    if detail is not None:
        assert response.json()["detail"] == detail
    assert len(store.writes) == writes
    assert "SITE_NAME" not in store.rows
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_falls_back_and_custom_variables_disappear(config_client) -> None:
    client, store, audit = config_client
    store.rows["SITE_NAME"] = row("SITE_NAME", "Stored")
    store.rows["BOX_REGION"] = row("BOX_REGION", "east")
    await app_config.refresh()

    response = await client.delete("/admin/config/SITE_NAME", headers=AUTH)
    assert response.status_code == 200
    entry = _entries(response.json())["SITE_NAME"]
    assert entry["source"] == "default"
    assert entry["value"] is None

    response = await client.delete("/admin/config/BOX_REGION", headers=AUTH)
    assert response.status_code == 200
    assert "BOX_REGION" not in _entries(response.json())
    assert [call.args[2] for call in audit.await_args_list] == ["config.reset", "config.reset"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,status",
    [("API_KEY_SECRET", 409), ("ERASURE_FENCE_SECRET", 409), ("SITE_NAME", 404), ("DB_HOST", 403)],
)
async def test_delete_refusals(config_client, key, status) -> None:
    client, _store, audit = config_client

    response = await client.delete(f"/admin/config/{key}", headers=AUTH)

    assert response.status_code == status
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_writes_without_a_database_answer_503(config_client) -> None:
    client, _store, _audit = config_client
    app_config.reset_state()
    app_config.use_environment(database_enabled=False)

    response = await client.patch(
        "/admin/config", json={"values": {"SITE_NAME": "x"}}, headers=AUTH
    )

    assert response.status_code == 503


# --- restart -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_is_refused_without_a_supervisor(config_client, monkeypatch) -> None:
    client, _store, audit = config_client
    monkeypatch.setattr(restart, "restart_supported", lambda: False)
    request_restart = MagicMock()
    monkeypatch.setattr(restart, "request_restart", request_restart)

    response = await client.post("/admin/system/restart", headers=AUTH)

    assert response.status_code == 409
    request_restart.assert_not_called()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_answers_202_then_signals(config_client, monkeypatch) -> None:
    client, _store, audit = config_client
    monkeypatch.setattr(restart, "restart_supported", lambda: True)
    kills: list[tuple[int, int]] = []
    exits: list[int] = []
    real_request_restart = restart.request_restart

    def request_restart() -> None:
        # The real function, with the signal and the watchdog's exit captured.
        real_request_restart(
            kill=lambda pid, sig: kills.append((pid, sig)),
            exit_process=exits.append,
            watchdog_seconds=3600,
        )

    monkeypatch.setattr(restart, "request_restart", request_restart)

    response = await client.post("/admin/system/restart", headers=AUTH)

    assert response.status_code == 202
    assert response.json() == {"restarting": True}
    assert kills == [(os.getpid(), signal.SIGTERM)]
    assert restart.restart_requested()
    assert exits == []  # the watchdog has not fired
    audit.assert_awaited_once()
    assert audit.await_args.args[2] == "system.restart"
