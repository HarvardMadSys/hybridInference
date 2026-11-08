"""Admin endpoints for the database-backed configuration and the backend restart."""

from __future__ import annotations

import os
import signal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from serving.audit import AuditActor, AuditTarget
from serving.config import app_config
from serving.config.settings import get_settings, settings
from serving.servers import restart
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import (
    config as config_routes,
    config_backup as config_backup_routes,
    system as system_routes,
)
from serving.storage.base import ProviderDefinitionRow, ProviderKeyRow
from serving.utils import password as password_utils
from serving.utils.login_rate_limit import reset_login_rate_limit_state
from tests.fixtures.app_config_store import FakeAppConfigStore, row

AUTH = {"Authorization": "Bearer test-admin"}


@pytest.fixture
async def config_client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("SMTP_PASSWORD", "env-smtp-password")
    monkeypatch.delenv("SITE_NAME", raising=False)
    # Tests below assert the fence secret is unset; CI's database job sets one.
    monkeypatch.delenv("ERASURE_FENCE_SECRET", raising=False)
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
    monkeypatch.setattr(config_backup_routes, "log_admin_action", audit)
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
    assert set(body) == {
        "categories",
        "entries",
        "missing",
        "pending_restart",
        "restart_supported",
        "source_version",
        "saved_version",
        "groups",
    }
    assert body["restart_supported"] is True
    assert {category["id"] for category in body["categories"]} >= {"email", "security"}
    entries = _entries(body)
    smtp = entries["SMTP_PASSWORD"]
    assert smtp["secret"] is True
    assert smtp["value"] is None
    assert smtp["default"] is None
    assert smtp["is_set"] is True
    assert smtp["source"] == "database"  # imported from the environment at boot
    assert smtp["one_time"] is True  # as is every Email setting
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
        "locked",
        "setup",
        "one_time",
        "origin",
        "custom",
        "invalid",
        "used_by",
        "updated_at",
        "updated_by",
        "group",
        "apply_mode",
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
async def test_get_leaves_out_settings_with_nothing_to_choose(config_client) -> None:
    client, _store, _audit = config_client

    body = (await client.get("/admin/config", headers=AUTH)).json()

    # No delivery extension is loaded: email has SMTP alone, and phone sign-in
    # has no SMS provider for its settings to tune.
    keys = set(_entries(body))
    assert "EMAIL_PROVIDER" not in keys
    assert not any(key.startswith("SMS_") for key in keys)
    assert {"SMTP_HOST", "SMTP_PASSWORD"} <= keys


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
    details = [call.kwargs["details"] for call in audit.await_args_list]
    assert {"key": "CHAT_UPSTREAM_KEY", "changed": True} in details
    assert {"key": "SITE_NAME", "old_value": None, "new_value": "Acme Gateway"} in details
    assert all(call.kwargs["action"] == "config.update" for call in audit.await_args_list)
    # Each change targets its own configuration key, typed, never a user.
    for call in audit.await_args_list:
        assert call.kwargs["target"] == AuditTarget("config", call.kwargs["details"]["key"])
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
    assert [call.kwargs["action"] for call in audit.await_args_list] == [
        "config.reset",
        "config.reset",
    ]


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
async def test_get_names_each_entrys_origin(config_client) -> None:
    client, store, _audit = config_client
    store.rows["BOX_REGION"] = row("BOX_REGION", "east")
    store.rows["MINIMAX_API_KEY2"] = row("MINIMAX_API_KEY2", "k2", secret=True)
    await app_config.refresh()

    entries = _entries((await client.get("/admin/config", headers=AUTH)).json())

    assert entries["SITE_NAME"]["origin"] == "static"
    assert entries["CHAT_UPSTREAM_KEY"]["origin"] == "discovered"
    assert entries["MINIMAX_API_KEY2"]["origin"] == "numbered"
    assert entries["BOX_REGION"]["origin"] == "custom"
    assert all(entry["custom"] is (entry["origin"] == "custom") for entry in entries.values())


@pytest.mark.asyncio
async def test_get_marks_an_immutable_setting_locked_once_it_is_settled(config_client) -> None:
    """The fence secret has no value of its own, but API_KEY_SECRET stands in for it."""
    client, _store, _audit = config_client

    entries = _entries((await client.get("/admin/config", headers=AUTH)).json())

    fence = entries["ERASURE_FENCE_SECRET"]
    assert fence["immutable"] is True and fence["is_set"] is False
    assert fence["locked"] is True
    assert entries["API_KEY_SECRET"]["locked"] is True
    assert entries["JWT_SECRET_KEY"]["locked"] is False
    assert entries["SITE_NAME"]["locked"] is False
    # Locked is exactly what a save of it answers with 409.
    response = await client.patch(
        "/admin/config", json={"values": {"ERASURE_FENCE_SECRET": "dedicated"}}, headers=AUTH
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_patch_marks_a_stored_variable_secret_without_a_value(config_client) -> None:
    client, store, audit = config_client
    store.rows["BOX_REGION"] = row("BOX_REGION", "east")
    await app_config.refresh()
    writes = len(store.writes)

    response = await client.patch(
        "/admin/config", json={"values": {}, "secrets": {"BOX_REGION": True}}, headers=AUTH
    )

    assert response.status_code == 200, response.text
    entry = _entries(response.json())["BOX_REGION"]
    assert entry["secret"] is True and entry["value"] is None
    assert store.rows["BOX_REGION"].secret
    assert store.rows["BOX_REGION"].value == "east"
    assert len(store.writes) == writes  # the value was not rewritten
    assert [call.kwargs["details"] for call in audit.await_args_list] == [
        {"key": "BOX_REGION", "marked_secret": True}
    ]
    assert '"east"' not in response.text

    # The values field may be left out.
    response = await client.patch(
        "/admin/config", json={"secrets": {"BOX_REGION": True}}, headers=AUTH
    )
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secrets,status,detail",
    [
        (
            {"BOX_REGION": True},
            400,
            "BOX_REGION has no stored value to protect yet: set a value and mark it secret in "
            "the same save.",
        ),
        (
            {"SITE_NAME": True},
            400,
            "SITE_NAME: a built-in setting keeps the registry's secret flag.",
        ),
        ({"DB_PASSWORD": True}, 403, None),
    ],
)
async def test_patch_refuses_a_secret_flag_it_cannot_honor(
    config_client, secrets, status, detail
) -> None:
    client, store, audit = config_client
    before = dict(store.rows)

    response = await client.patch(
        "/admin/config", json={"values": {}, "secrets": secrets}, headers=AUTH
    )

    assert response.status_code == status, response.text
    if detail is not None:
        assert response.json()["detail"] == detail
    assert store.rows == before
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
    assert response.json() == {"restarting": True, "instance": restart.INSTANCE_ID}
    assert kills == [(os.getpid(), signal.SIGTERM)]
    assert restart.restart_requested()
    assert exits == []  # the watchdog has not fired
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "system.restart"
    assert audit.await_args.kwargs["actor"].type == "admin_token"


@pytest.mark.asyncio
async def test_backup_requires_an_administrator(config_client) -> None:
    client, _store, _audit = config_client

    assert (await client.get("/admin/config/backup")).status_code == 401
    assert (
        await client.post("/admin/config/backup", json={"include_secrets": True})
    ).status_code == 401
    assert (
        await client.post("/admin/config/restore", json={"schema_version": 1})
    ).status_code == 401
    assert (
        await client.post("/admin/config/restore/preview", json={"schema_version": 1})
    ).status_code == 401


ADMIN_PASSWORD = "correct-admin-password"


@pytest.fixture
def console_admin(config_client, monkeypatch):
    """The same app, with an administrator signed in to the console instead of ADMIN_TOKEN.

    The account has a password, checked by the real re-authentication with a
    stand-in hash, its attempts counted against the real sign-in limits.
    """
    client, store, audit = config_client
    app = client._transport.app

    async def console_session(request: Request) -> str:
        request.state.admin_user_id = "u-admin"
        request.state.audit_actor = AuditActor.admin("u-admin", "127.0.0.1")
        return "admin@example.com"

    app.dependency_overrides[verify_admin_access] = console_session
    account: dict[str, Any] = {
        "id": "u-admin",
        "email": "admin@example.com",
        "password_hash": "stored-hash",
    }
    app.state.services.operational_store.get_user_by_id = AsyncMock(return_value=account)
    monkeypatch.setattr(
        password_utils, "verify_password", lambda plain, _hash: plain == ADMIN_PASSWORD
    )
    for name, value in (
        ("login_rate_limit_per_15min", 5),
        ("login_account_rate_limit_per_15min", 50),
        ("login_rate_limit_per_hour_per_ip", 20),
    ):
        monkeypatch.setattr(settings, name, value)
    reset_login_rate_limit_state()
    yield client, store, audit, account
    reset_login_rate_limit_state()


def _stock_backup_sources(op_store) -> None:
    """Give the mocked operational store one row of every secret kind a backup carries."""
    op_store.list_settings = AsyncMock(
        return_value=[
            {"key": "setup_completed_at", "value": "2026-10-01T00:00:00Z", "value_type": "str"},
            {"key": "setup_code", "value": "secret-setup-code", "value_type": "str"},
            {"key": "signup_enabled", "value": "true", "value_type": "bool"},
            {"key": "usage_insights_api_key", "value": "sk-insights-secret", "value_type": "str"},
        ]
    )
    op_store.list_provider_definitions = AsyncMock(
        return_value=[
            ProviderDefinitionRow(
                provider="acme",
                display_name="Acme AI",
                adapter_kind="openai_compat",
                default_base_url="https://api.acme.test/v1",
                status="active",
                created_at=None,
                updated_at=None,
            )
        ]
    )
    op_store.list_provider_keys = AsyncMock(
        return_value=[
            ProviderKeyRow(
                id="key-1",
                provider="acme",
                key_prefix="sk-acme...",
                label="primary",
                status="active",
                created_at=None,
                min_role="pro",
            )
        ]
    )
    op_store.list_provider_key_values = AsyncMock(return_value={"key-1": "sk-acme-plaintext-1234"})
    op_store.get_provider_key_full = AsyncMock(return_value=("acme", "sk-acme-plaintext-1234"))


@pytest.mark.asyncio
async def test_backup_names_secrets_without_their_values_and_excludes_setup_markers(
    config_client,
) -> None:
    client, store, audit = config_client
    await client.patch(
        "/admin/config",
        headers=AUTH,
        json={"values": {"SITE_NAME": "Backup Lab", "CHAT_UPSTREAM_KEY": "sk-chat-secret"}},
    )
    audit.reset_mock()
    op_store = client._transport.app.state.services.operational_store
    _stock_backup_sources(op_store)

    response = await client.get("/admin/config/backup", headers=AUTH)

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    assert body["schema_version"] == 1
    assert body["secrets_included"] is False
    # Not one stored secret, under any name.
    for value in [
        "env-smtp-password",
        "sk-chat-secret",
        "sk-insights-secret",
        "sk-acme-plaintext-1234",
        *(stored.value for stored in store.rows.values() if stored.secret),
    ]:
        assert value not in response.text
    op_store.list_provider_key_values.assert_not_awaited()
    op_store.get_provider_key_full.assert_not_awaited()

    app_rows = {r["key"]: r for r in body["app_config"]}
    assert app_rows["SITE_NAME"]["value"] == "Backup Lab"
    assert app_rows["SITE_NAME"]["is_set"] is True
    for key in ("CHAT_UPSTREAM_KEY", "SMTP_PASSWORD", "JWT_SECRET_KEY", "API_KEY_SECRET"):
        assert app_rows[key]["value"] is None
        assert app_rows[key]["secret"] is True
        assert app_rows[key]["is_set"] is True

    settings_by_key = {s["key"]: s for s in body["site_settings"]}
    assert list(settings_by_key) == ["signup_enabled", "usage_insights_api_key"]
    assert settings_by_key["signup_enabled"]["value"] == "true"
    assert settings_by_key["usage_insights_api_key"]["value"] is None

    assert body["provider_definitions"] == [
        {
            "provider": "acme",
            "display_name": "Acme AI",
            "adapter_kind": "openai_compat",
            "default_base_url": "https://api.acme.test/v1",
            "status": "active",
        }
    ]
    assert body["provider_keys"] == [
        {
            "id": "key-1",
            "provider": "acme",
            "api_key": None,
            "label": "primary",
            "status": "active",
            "min_role": "pro",
        }
    ]
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "config.backup"
    assert audit.await_args.kwargs["details"]["secrets_included"] is False
    assert audit.await_args.kwargs["actor"].type == "admin_token"
    assert audit.await_args.kwargs["actor"].id is None
    assert audit.await_args.kwargs["target"] == AuditTarget("config", "backup")

    # Asked for without secrets, POST is the same bundle and needs no password.
    plain = await client.post("/admin/config/backup", headers=AUTH, json={})
    assert plain.status_code == 200
    assert plain.json()["secrets_included"] is False
    assert "sk-acme-plaintext-1234" not in plain.text


@pytest.mark.asyncio
async def test_the_admin_token_cannot_export_secret_values(config_client) -> None:
    client, _store, audit = config_client
    _stock_backup_sources(client._transport.app.state.services.operational_store)

    response = await client.post(
        "/admin/config/backup",
        headers=AUTH,
        json={"include_secrets": True, "password": "anything"},
    )

    assert response.status_code == 403
    assert "admin token cannot confirm" in response.json()["detail"]
    assert "sk-acme-plaintext-1234" not in response.text
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_backup_with_secret_values_needs_the_administrators_password(
    console_admin,
) -> None:
    client, _store, audit, _account = console_admin
    await client.patch(
        "/admin/config", headers=AUTH, json={"values": {"CHAT_UPSTREAM_KEY": "sk-chat-secret"}}
    )
    audit.reset_mock()
    _stock_backup_sources(client._transport.app.state.services.operational_store)

    for body in ({"include_secrets": True}, {"include_secrets": True, "password": "wrong"}):
        refused = await client.post("/admin/config/backup", headers=AUTH, json=body)
        assert refused.status_code == 400
        assert refused.json()["detail"] == "Current password is incorrect."
        assert "sk-acme-plaintext-1234" not in refused.text
    audit.assert_not_awaited()

    response = await client.post(
        "/admin/config/backup",
        headers=AUTH,
        json={"include_secrets": True, "password": ADMIN_PASSWORD},
    )

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    assert body["secrets_included"] is True
    app_rows = {r["key"]: r for r in body["app_config"]}
    assert app_rows["CHAT_UPSTREAM_KEY"]["value"] == "sk-chat-secret"
    assert app_rows["SMTP_PASSWORD"]["value"] == "env-smtp-password"
    assert {s["key"]: s["value"] for s in body["site_settings"]}["usage_insights_api_key"] == (
        "sk-insights-secret"
    )
    assert body["provider_keys"][0]["api_key"] == "sk-acme-plaintext-1234"
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["details"]["secrets_included"] is True
    assert audit.await_args.kwargs["actor"].id == "u-admin"


@pytest.mark.asyncio
async def test_an_account_without_a_password_cannot_export_secret_values(console_admin) -> None:
    client, _store, _audit, account = console_admin
    account["password_hash"] = None

    response = await client.post(
        "/admin/config/backup", headers=AUTH, json={"include_secrets": True, "password": "x"}
    )

    assert response.status_code == 400
    assert "this account has none" in response.json()["detail"]


async def _exported(client) -> dict[str, Any]:
    response = await client.get("/admin/config/backup", headers=AUTH)
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_restoring_a_bundle_without_secret_values_keeps_this_deployments_secrets(
    config_client,
) -> None:
    """A withheld secret is never blanked; one with nothing to keep is named, not invented."""
    client, store, audit = config_client
    await client.patch(
        "/admin/config", headers=AUTH, json={"values": {"CHAT_UPSTREAM_KEY": "sk-chat-secret"}}
    )
    op_store = client._transport.app.state.services.operational_store
    _stock_backup_sources(op_store)
    op_store.delete_setting = AsyncMock()
    op_store.set_setting = AsyncMock()
    op_store.delete_provider_key = AsyncMock()
    op_store.add_provider_key = AsyncMock()
    bundle = await _exported(client)
    # A secret this deployment never had, and a key it does not hold.
    bundle["app_config"].append(
        {"key": "TURNSTILE_SECRET_KEY", "value": None, "secret": True, "source": "admin"}
    )
    bundle["provider_keys"].append(
        {"id": "key-gone", "provider": "acme", "api_key": None, "label": "old", "min_role": "free"}
    )
    op_store.get_provider_key_full = AsyncMock(
        side_effect=lambda key_id: ("acme", "sk-acme-plaintext-1234") if key_id == "key-1" else None
    )
    secrets_before = {key: stored.value for key, stored in store.rows.items() if stored.secret}
    keys_before = set(store.rows)
    audit.reset_mock()

    preview = await client.post("/admin/config/restore/preview", headers=AUTH, json=bundle)
    assert preview.status_code == 200
    assert preview.json() == {
        "security_changes": [],
        "secrets_not_restored": [
            "app_config:TURNSTILE_SECRET_KEY",
            "provider_keys:acme/key-gone",
        ],
    }

    response = await client.post("/admin/config/restore", headers=AUTH, json=bundle)

    assert response.status_code == 200
    body = response.json()
    assert body["security_changes"] == []
    assert body["secrets_not_restored"] == [
        "app_config:TURNSTILE_SECRET_KEY",
        "provider_keys:acme/key-gone",
    ]
    assert {key: stored.value for key, stored in store.rows.items() if stored.secret} == (
        secrets_before
    )
    assert set(store.rows) == keys_before
    assert "TURNSTILE_SECRET_KEY" not in store.rows
    # The withheld usage-insights key is neither rewritten nor deleted.
    op_store.set_setting.assert_awaited_once_with("signup_enabled", "true", "bool", "admin-token")
    op_store.delete_setting.assert_not_awaited()
    # The provider key comes back with this deployment's value; the other is left out.
    op_store.add_provider_key.assert_awaited_once_with(
        provider="acme",
        api_key="sk-acme-plaintext-1234",
        label="primary",
        created_by="admin-token",
        key_id="key-1",
        min_role="pro",
    )
    details = audit.await_args.kwargs["details"]
    assert details["secrets_included"] is False
    assert details["secrets_not_restored"] == body["secrets_not_restored"]


@pytest.mark.asyncio
async def test_a_withheld_value_is_accepted_only_for_a_secret(config_client) -> None:
    client, _store, _audit = config_client

    for bundle in (
        {
            "schema_version": 1,
            "app_config": [{"key": "SITE_NAME", "value": None, "secret": False}],
        },
        {
            "schema_version": 1,
            "site_settings": [{"key": "signup_enabled", "value": None, "value_type": "bool"}],
        },
    ):
        response = await client.post("/admin/config/restore/preview", headers=AUTH, json=bundle)
        assert response.status_code == 400, bundle


@pytest.mark.asyncio
async def test_restore_replaces_the_signup_domain_allowlist_and_refreshes_resolvers(
    console_admin,
) -> None:
    """An empty allowlist approves every signup, so a restore must not drop it."""
    client, _store, _audit, _account = console_admin
    services = client._transport.app.state.services
    op_store = services.operational_store
    op_store.list_signup_allowed_domains = AsyncMock(
        return_value=[
            {"domain": "harvard.edu", "is_wildcard": False},
            {"domain": "old.org", "is_wildcard": False},
        ]
    )
    op_store.add_signup_allowed_domain = AsyncMock()
    op_store.remove_signup_allowed_domain = AsyncMock()
    services.model_visibility_resolver = MagicMock(spec=["invalidate_cache"])
    services.model_concurrency_resolver = MagicMock(spec=["invalidate_cache"])
    bundle = await _exported(client)
    bundle["signup_allowed_domains"] = [
        {"domain": "harvard.edu", "is_wildcard": False},
        {"domain": "mit.edu", "is_wildcard": True},
    ]

    response = await client.post(
        "/admin/config/restore",
        headers=AUTH,
        json={
            **bundle,
            "confirm_security_changes": ["signup_allowed_domains"],
            "password": ADMIN_PASSWORD,
        },
    )

    assert response.status_code == 200
    assert response.json()["security_changes"] == ["signup_allowed_domains"]
    op_store.remove_signup_allowed_domain.assert_awaited_once_with(
        domain="old.org", is_wildcard=False
    )
    # Attributed to the console administrator's account.
    op_store.add_signup_allowed_domain.assert_awaited_once_with(
        domain="mit.edu", is_wildcard=True, created_by="u-admin"
    )
    services.model_visibility_resolver.invalidate_cache.assert_called_once_with()
    services.model_concurrency_resolver.invalidate_cache.assert_called_once_with()


@pytest.mark.asyncio
async def test_restore_accepts_a_paused_batch_model_its_backup_exported(config_client) -> None:
    """A cap of 0 pauses a model's batch work; a backup taken meanwhile holds it."""
    client, _store, _audit = config_client
    services = client._transport.app.state.services
    services.batch_store = MagicMock()
    services.batch_store.list_model_limits = AsyncMock(return_value={})
    services.batch_store.set_model_limit = AsyncMock()
    bundle = await _exported(client)
    bundle["batch_model_limits"] = {"glm-4.6": 0}

    response = await client.post("/admin/config/restore", headers=AUTH, json=bundle)

    assert response.status_code == 200
    services.batch_store.set_model_limit.assert_awaited_once_with(
        "glm-4.6", 0, updated_by="admin-token"
    )


def _security_bundle() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "app_config": [
            {"key": "SITE_NAME", "value": "Restored Gateway", "secret": False, "source": "admin"},
            {
                "key": "API_KEY_SECRET",
                "value": "different-backup-secret-that-must-not-break-keys",
                "secret": True,
                "source": "generated",
            },
            {
                "key": "CHAT_UPSTREAM_KEY",
                "value": "sk-restored-upstream",
                "secret": True,
                "source": "admin",
            },
        ],
        "site_settings": [
            {"key": "signup_enabled", "value": "true", "value_type": "bool"},
        ],
    }


@pytest.mark.asyncio
async def test_security_changes_need_confirmation_and_the_password(console_admin) -> None:
    """An unsigned bundle may not rewrite secrets or trust settings on a session alone."""
    client, store, audit, _account = console_admin
    store.has_api_keys = True
    op_store = client._transport.app.state.services.operational_store
    op_store.list_settings = AsyncMock(
        return_value=[{"key": "signup_enabled", "value": "true", "value_type": "bool"}]
    )
    op_store.set_setting = AsyncMock()
    before = dict(store.rows)
    bundle = _security_bundle()

    preview = await client.post("/admin/config/restore/preview", headers=AUTH, json=bundle)
    assert preview.status_code == 200
    changes = preview.json()["security_changes"]
    # Every security-relevant row the bundle leaves out is deleted, so it is listed.
    assert "app_config:CHAT_UPSTREAM_KEY" in changes
    assert "app_config:SMTP_PASSWORD" in changes
    assert "app_config:SITE_NAME" not in changes
    # Kept because user API keys exist: not a change.
    assert "app_config:API_KEY_SECRET" not in changes
    assert changes == sorted(changes)

    refusals = [
        ({}, 409),
        ({"confirm_security_changes": changes[:-1], "password": ADMIN_PASSWORD}, 409),
        ({"confirm_security_changes": changes}, 400),
        ({"confirm_security_changes": changes, "password": "wrong"}, 400),
    ]
    for extra, status in refusals:
        refused = await client.post("/admin/config/restore", headers=AUTH, json={**bundle, **extra})
        assert refused.status_code == status, extra
        assert store.rows == before
    op_store.set_setting.assert_not_awaited()
    audit.assert_not_awaited()
    assert "app_config:CHAT_UPSTREAM_KEY" in refused_detail(
        await client.post("/admin/config/restore", headers=AUTH, json=bundle)
    )

    response = await client.post(
        "/admin/config/restore",
        headers=AUTH,
        json={**bundle, "confirm_security_changes": changes, "password": ADMIN_PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["security_changes"] == changes
    assert body["immutable_keys_preserved"] == ["API_KEY_SECRET"]
    assert store.rows["API_KEY_SECRET"].value == before["API_KEY_SECRET"].value
    assert store.rows["SITE_NAME"].value == "Restored Gateway"
    assert store.rows["CHAT_UPSTREAM_KEY"].value == "sk-restored-upstream"
    entries = _entries(body["config"])
    assert entries["CHAT_UPSTREAM_KEY"]["value"] is None
    assert entries["CHAT_UPSTREAM_KEY"]["is_set"] is True
    op_store.set_setting.assert_awaited_once_with(
        "signup_enabled", "true", "bool", "admin@example.com"
    )
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "config.restore"
    assert audit.await_args.kwargs["actor"].id == "u-admin"
    assert audit.await_args.kwargs["details"]["security_changes"] == changes
    assert audit.await_args.kwargs["target"] == AuditTarget("config", "backup")


def refused_detail(response) -> str:
    assert response.status_code == 409
    return response.json()["detail"]


@pytest.mark.asyncio
async def test_the_admin_token_cannot_restore_security_changes(config_client) -> None:
    client, store, audit = config_client
    store.has_api_keys = True
    before = dict(store.rows)
    bundle = _security_bundle()
    changes = (
        await client.post("/admin/config/restore/preview", headers=AUTH, json=bundle)
    ).json()["security_changes"]

    response = await client.post(
        "/admin/config/restore",
        headers=AUTH,
        json={**bundle, "confirm_security_changes": changes, "password": "anything"},
    )

    assert response.status_code == 403
    assert store.rows == before
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_backup_leaves_out_the_retired_routewise_latency_threshold(config_client) -> None:
    # routewise_latency_min_samples was removed; its rows may still be stored,
    # and a backup taken while it existed must still restore.
    client, _store, _audit = config_client
    retired = [
        {"key": "routewise_latency_min_samples", "value": "10", "value_type": "int"},
        {
            "key": "model_routewise_setting:routewise_latency_min_samples:chat",
            "value": "4",
            "value_type": "int",
        },
    ]
    op_store = client._transport.app.state.services.operational_store
    op_store.list_settings = AsyncMock(
        return_value=[*retired, {"key": "signup_enabled", "value": "true", "value_type": "bool"}]
    )
    op_store.delete_setting = AsyncMock()
    op_store.set_setting = AsyncMock()

    exported = await _exported(client)
    assert [s["key"] for s in exported["site_settings"]] == ["signup_enabled"]

    response = await client.post(
        "/admin/config/restore",
        headers=AUTH,
        json={
            **exported,
            "site_settings": [
                *retired,
                {"key": "signup_enabled", "value": "true", "value_type": "bool"},
            ],
        },
    )

    assert response.status_code == 200
    op_store.set_setting.assert_awaited_once_with("signup_enabled", "true", "bool", "admin-token")
    # The rows left behind are cleared rather than kept.
    assert {call.args[0] for call in op_store.delete_setting.await_args_list} == {
        row["key"] for row in retired
    }


@pytest.mark.asyncio
async def test_restore_overwrites_generated_api_key_secret_when_no_user_keys_exist(
    console_admin,
    monkeypatch,
) -> None:
    client, store, _audit, _account = console_admin
    monkeypatch.delenv("API_KEY_SECRET", raising=False)
    store.has_api_keys = False
    store.rows["API_KEY_SECRET"] = row(
        "API_KEY_SECRET",
        "fresh-auto-generated-secret-00000000",
        secret=True,
        source="generated",
    )
    bundle = await _exported(client)
    bundle["app_config"] = [r for r in bundle["app_config"] if r["key"] != "API_KEY_SECRET"] + [
        {
            "key": "API_KEY_SECRET",
            "value": "migrated-api-key-secret-from-backup-12345",
            "secret": True,
            "source": "generated",
        }
    ]
    preview = await client.post("/admin/config/restore/preview", headers=AUTH, json=bundle)
    assert preview.json()["security_changes"] == ["app_config:API_KEY_SECRET"]

    response = await client.post(
        "/admin/config/restore",
        headers=AUTH,
        json={
            **bundle,
            "confirm_security_changes": ["app_config:API_KEY_SECRET"],
            "password": ADMIN_PASSWORD,
        },
    )

    assert response.status_code == 200
    assert response.json()["immutable_keys_preserved"] == []
    assert store.rows["API_KEY_SECRET"].value == "migrated-api-key-secret-from-backup-12345"


@pytest.mark.asyncio
async def test_restore_rejects_invalid_payload_without_changing_store(config_client) -> None:
    client, store, audit = config_client
    before = dict(store.rows)

    bad_payload = {
        "schema_version": 1,
        "app_config": [
            {"key": "SMTP_PORT", "value": "not-an-int", "secret": False, "source": "admin"},
        ],
    }

    response = await client.post("/admin/config/restore", headers=AUTH, json=bad_payload)

    assert response.status_code == 400
    assert "SMTP_PORT" in response.json()["detail"]
    assert store.rows == before
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failure_in_another_store_rolls_the_configuration_back(config_client) -> None:
    """The other tables are written inside the app_config transaction: all or nothing."""
    client, store, audit = config_client
    op_store = client._transport.app.state.services.operational_store
    op_store.set_setting = AsyncMock(side_effect=ValueError("site_settings write refused"))
    bundle = await _exported(client)
    bundle["app_config"].append(
        {"key": "SITE_NAME", "value": "Never Applied", "secret": False, "source": "admin"}
    )
    bundle["site_settings"] = [
        {"key": "analytics_growth_rate_enabled", "value": "true", "value_type": "bool"}
    ]
    before = dict(store.rows)
    audit.reset_mock()

    response = await client.post("/admin/config/restore", headers=AUTH, json=bundle)

    assert response.status_code == 400
    assert store.rows == before
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_preview_names_where_requests_and_keys_would_go(config_client) -> None:
    """A provider key's value, a route's target or a provider's base URL is a security change."""
    client, _store, _audit = config_client
    op_store = client._transport.app.state.services.operational_store
    _stock_backup_sources(op_store)
    route = {
        "model_id": "chat",
        "route_id": "r1",
        "route_type": "quota",
        "provider": "acme",
        "openrouter_sort": None,
        "deployment_type": "auto",
        "base_url": "https://api.acme.test/v1",
        "api_key_id": "key-1",
        "provider_model_id": "acme-chat",
        "quota_limit": None,
        "concurrency_limit": None,
        "weight": 1.0,
        "pricing": None,
        "capabilities": None,
        "endpoint_id": None,
    }
    op_store.list_all_provider_route_candidates = AsyncMock(return_value=[route])
    bundle = await _exported(client)
    bundle["provider_keys"][0]["api_key"] = "sk-acme-plaintext-1234"

    async def preview() -> list[str]:
        response = await client.post("/admin/config/restore/preview", headers=AUTH, json=bundle)
        assert response.status_code == 200
        return response.json()["security_changes"]

    # The same value and the same targets: nothing to confirm.
    assert await preview() == []

    bundle["provider_keys"][0]["api_key"] = "sk-acme-someone-elses"
    bundle["provider_route_candidates"][0]["base_url"] = "https://collector.example/v1"
    bundle["provider_definitions"][0]["default_base_url"] = "https://collector.example/v1"
    assert await preview() == [
        "provider_definitions:acme",
        "provider_keys:acme/key-1",
        "provider_route_candidates:chat/r1",
    ]


@pytest.mark.asyncio
async def test_a_backup_carries_the_batch_route_choices_and_a_restore_puts_them_back(
    config_client,
) -> None:
    """Which routes batch work may use is configuration: backed up, and replaced on restore."""
    client, _store, _audit = config_client
    op_store = client._transport.app.state.services.operational_store
    op_store.list_all_provider_route_batch_policies = AsyncMock(
        return_value=[
            {"model_id": "chat", "route_id": "chat:local-1", "batch_allowed": False},
            {"model_id": "chat", "route_id": "chat:api-api", "batch_allowed": True},
        ]
    )
    op_store.set_provider_route_batch_policy = AsyncMock()

    bundle = await _exported(client)

    assert bundle["provider_route_batch_policies"] == [
        {"model_id": "chat", "route_id": "chat:api-api", "batch_allowed": True},
        {"model_id": "chat", "route_id": "chat:local-1", "batch_allowed": False},
    ]

    bundle["provider_route_batch_policies"] = [
        {"model_id": "chat", "route_id": "chat:api-api", "batch_allowed": False}
    ]
    response = await client.post("/admin/config/restore", headers=AUTH, json=bundle)

    assert response.status_code == 200, response.text
    calls = op_store.set_provider_route_batch_policy.await_args_list
    # A stored choice the bundle leaves out is cleared: that route follows its locality.
    assert any(
        c.args == ("chat", "chat:local-1") and c.kwargs["batch_allowed"] is None for c in calls
    )
    assert any(
        c.args == ("chat", "chat:api-api") and c.kwargs["batch_allowed"] is False for c in calls
    )
