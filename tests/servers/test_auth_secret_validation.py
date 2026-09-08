"""Regression coverage for missing authentication secrets at startup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.runtime_settings import RuntimeSettings
from serving.config.settings import get_settings
from serving.servers import app as app_module
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers.admin import settings as settings_routes


@pytest.fixture
def auth_environment(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-not-for-production")
    monkeypatch.setenv("API_KEY_SECRET", "test-api-secret-not-for-production")
    monkeypatch.setenv("ADMIN_TOKEN", "")
    monkeypatch.setenv("USER_AUTH_ENABLED", "true")
    monkeypatch.setenv("DB_ENABLED", "true")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "db_enabled,user_auth_enabled", [(True, True), (True, False), (False, True)]
)
@pytest.mark.parametrize("missing_key", ["JWT_SECRET_KEY", "API_KEY_SECRET"])
@pytest.mark.parametrize("blank", ["", " \t\n"])
async def test_missing_auth_secret_rejects_startup_before_services_are_created(
    monkeypatch, auth_environment, db_enabled, user_auth_enabled, missing_key, blank
):
    monkeypatch.setenv("DB_ENABLED", str(db_enabled))
    monkeypatch.setenv("USER_AUTH_ENABLED", str(user_auth_enabled))
    monkeypatch.setenv(missing_key, blank)
    get_settings.cache_clear()
    initialize = AsyncMock(return_value=object())
    shutdown = AsyncMock()
    monkeypatch.setattr(app_module.bootstrap, "initialize", initialize)
    monkeypatch.setattr(app_module.bootstrap, "shutdown", shutdown)
    app = FastAPI()

    with pytest.raises(ValueError, match=missing_key):
        async with app_module.lifespan(app):
            pass

    initialize.assert_not_awaited()
    shutdown.assert_not_awaited()
    assert not hasattr(app.state, "services")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "db_enabled,user_auth_enabled", [(True, True), (True, False), (False, True)]
)
async def test_configured_secrets_allow_startup_without_legacy_admin_token(
    monkeypatch, auth_environment, db_enabled, user_auth_enabled
):
    monkeypatch.setenv("DB_ENABLED", str(db_enabled))
    monkeypatch.setenv("USER_AUTH_ENABLED", str(user_auth_enabled))
    services = object()
    initialize = AsyncMock(return_value=services)
    shutdown = AsyncMock()
    monkeypatch.setattr(app_module.bootstrap, "initialize", initialize)
    monkeypatch.setattr(app_module.bootstrap, "shutdown", shutdown)
    app = FastAPI()

    async with app_module.lifespan(app):
        assert app.state.services is services
        shutdown.assert_not_awaited()

    initialize.assert_awaited_once()
    shutdown.assert_awaited_once_with(services)


@pytest.mark.asyncio
async def test_explicit_database_free_auth_disabled_gateway_needs_no_secrets(
    monkeypatch, auth_environment, caplog
):
    monkeypatch.setenv("DB_ENABLED", "false")
    monkeypatch.setenv("USER_AUTH_ENABLED", "false")
    monkeypatch.delenv("JWT_SECRET_KEY")
    monkeypatch.delenv("API_KEY_SECRET")
    services = object()
    monkeypatch.setattr(app_module.bootstrap, "initialize", AsyncMock(return_value=services))
    shutdown = AsyncMock()
    monkeypatch.setattr(app_module.bootstrap, "shutdown", shutdown)
    app = FastAPI()

    async with app_module.lifespan(app):
        assert app.state.services is services

    shutdown.assert_awaited_once_with(services)
    assert "insecure" not in caplog.text


@pytest.mark.asyncio
async def test_dotenv_is_loaded_before_deciding_whether_secrets_are_required(
    monkeypatch, auth_environment, tmp_path
):
    from dotenv.main import load_dotenv

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("DB_ENABLED=false\nUSER_AUTH_ENABLED=false\n")
    for key in ("DB_ENABLED", "USER_AUTH_ENABLED", "JWT_SECRET_KEY", "API_KEY_SECRET"):
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "load_dotenv", lambda: load_dotenv(dotenv_path))
    monkeypatch.setattr(app_module.bootstrap, "initialize", AsyncMock(return_value=object()))
    monkeypatch.setattr(app_module.bootstrap, "shutdown", AsyncMock())

    async with app_module.lifespan(FastAPI()):
        pass


@pytest.mark.asyncio
async def test_default_mode_reports_all_missing_secrets(monkeypatch, auth_environment):
    for key in ("DB_ENABLED", "USER_AUTH_ENABLED", "JWT_SECRET_KEY", "API_KEY_SECRET"):
        monkeypatch.delenv(key)
    initialize = AsyncMock()
    monkeypatch.setattr(app_module.bootstrap, "initialize", initialize)

    with pytest.raises(ValueError, match="JWT_SECRET_KEY, API_KEY_SECRET"):
        async with app_module.lifespan(FastAPI()):
            pass

    initialize.assert_not_awaited()


@pytest.fixture
async def settings_client(monkeypatch, auth_environment):
    # Exercise validation after administrator authentication. The existing
    # test_admin_settings suite separately covers the real auth dependency.
    store = MagicMock()
    store.get_setting = AsyncMock(return_value={"value": "false", "value_type": "bool"})
    store.set_setting = AsyncMock()
    runtime_settings = RuntimeSettings(store)
    await runtime_settings.get_bool("user_auth_enabled")
    log_action = AsyncMock()
    monkeypatch.setattr(settings_routes, "log_admin_action", log_action)
    app = FastAPI()
    app.include_router(settings_routes.router)
    app.dependency_overrides[verify_admin_access] = lambda: "test-admin"
    app.state.services = AppServices(
        router=MagicMock(), operational_store=store, runtime_settings=runtime_settings
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store, runtime_settings, log_action


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_key", ["JWT_SECRET_KEY", "API_KEY_SECRET"])
@pytest.mark.parametrize("blank", ["", " \t\n"])
async def test_enabling_auth_with_missing_secret_does_not_change_state(
    monkeypatch, settings_client, missing_key, blank
):
    client, store, runtime_settings, log_action = settings_client
    monkeypatch.setenv("USER_AUTH_ENABLED", "false")
    monkeypatch.setenv(missing_key, blank)
    get_settings.cache_clear()

    response = await client.patch("/admin/settings/user_auth_enabled", json={"value": True})

    assert response.status_code == 400
    assert missing_key in response.json()["detail"]
    assert "test-jwt-secret-not-for-production" not in response.text
    assert "test-api-secret-not-for-production" not in response.text
    store.set_setting.assert_not_awaited()
    log_action.assert_not_awaited()
    assert runtime_settings.get_cached("user_auth_enabled") == (True, False)


@pytest.mark.asyncio
async def test_enabling_auth_with_configured_secrets_updates_setting(settings_client):
    client, store, runtime_settings, log_action = settings_client

    response = await client.patch("/admin/settings/user_auth_enabled", json={"value": True})

    assert response.status_code == 200
    store.set_setting.assert_awaited_once_with("user_auth_enabled", "True", "bool", "test-admin")
    log_action.assert_awaited_once()
    assert runtime_settings.get_cached("user_auth_enabled") == (False, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["user_auth_enabled", "signup_enabled"])
async def test_disabling_features_does_not_require_missing_secrets(
    monkeypatch, settings_client, key
):
    client, store, _, _ = settings_client
    monkeypatch.setenv("JWT_SECRET_KEY", "")
    monkeypatch.setenv("API_KEY_SECRET", "")
    get_settings.cache_clear()

    response = await client.patch(f"/admin/settings/{key}", json={"value": False})

    assert response.status_code == 200
    store.set_setting.assert_awaited_once_with(key, "False", "bool", "test-admin")
