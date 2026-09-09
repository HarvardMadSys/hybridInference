"""The signup API and public configuration must expose the same policy."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.auth.signup_policy import invalidate_allowlist_cache
from serving.config.distribution import get_distribution_config
from serving.config.runtime_settings import init_runtime_settings
from serving.config.settings import get_settings
from serving.servers.deps import get_db_logger, get_operational_store
from serving.servers.routers import auth_routes, site_config
from tests.fixtures.auth_factories import create_signup_request


@pytest.fixture
async def signup_client(monkeypatch):
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_MODE", raising=False)
    monkeypatch.setenv("SIGNUP_ENABLED", "true")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    invalidate_allowlist_cache()
    store = AsyncMock()
    store.get_setting.return_value = None
    store.get_user_by_email.return_value = None
    store.signup_allowlist_is_empty.return_value = True
    monkeypatch.setattr(auth_routes, "is_email_enabled", lambda: False)
    monkeypatch.setattr(auth_routes, "log_admin_action", AsyncMock())
    monkeypatch.setattr(
        auth_routes, "check_and_record_signup", AsyncMock(return_value=(True, None))
    )
    monkeypatch.setattr(auth_routes, "verify_turnstile_token", AsyncMock(return_value=True))
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(site_config.router)
    app.dependency_overrides[get_operational_store] = lambda: store
    app.dependency_overrides[get_db_logger] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store
    invalidate_allowlist_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,manifest_flag,env_enabled,runtime_override,expected",
    [
        ("active", "false", True, None, False),
        ("active", "false", True, True, False),
        ("active", "false", False, True, False),
        ("active", "true", False, None, False),
        ("active", "true", True, False, False),
        ("active", "true", False, True, True),
        ("active", "true", True, None, True),
        ("active", "null", False, None, False),
        ("active", "null", True, None, True),
        ("active", None, True, False, False),
        ("active", None, True, None, True),
        ("dark", "false", True, None, True),
        ("typo", "false", True, None, True),
        (None, None, False, None, False),
        (None, None, True, False, False),
        (None, None, False, True, True),
        (None, None, True, None, True),
    ],
)
async def test_signup_policy_matches_public_configuration(
    signup_client,
    monkeypatch,
    tmp_path,
    mode,
    manifest_flag,
    env_enabled,
    runtime_override,
    expected,
):
    client, store = signup_client
    monkeypatch.setenv("SIGNUP_ENABLED", str(env_enabled))
    if mode is not None:
        manifest = tmp_path / "distribution.yaml"
        contents = "schema_version: 1\ndistribution: {id: policy-test}\n"
        if manifest_flag is not None:
            contents += f"features:\n  public_signup: {manifest_flag}\n"
        manifest.write_text(contents)
        monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
        monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", mode)
    get_settings.cache_clear()
    if runtime_override is not None:
        store.get_setting.side_effect = lambda key: (
            {"value": str(runtime_override), "value_type": "bool"}
            if key == "signup_enabled"
            else None
        )
    init_runtime_settings(store)

    configuration = await client.get("/site-config")
    response = await client.post("/auth/signup", json=create_signup_request())

    assert response.status_code == (201 if expected else 403)
    assert configuration.status_code == 200
    assert configuration.json()["features"]["public_signup"] is expected
    if expected:
        store.create_user.assert_awaited_once()
    else:
        assert "Public signup is currently disabled" in response.json()["detail"]
        store.create_user.assert_not_awaited()
        auth_routes.check_and_record_signup.assert_not_awaited()
        auth_routes.verify_turnstile_token.assert_not_awaited()
    if mode == "active" and manifest_flag == "false":
        store.get_setting.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_signup_policy_without_runtime_settings(signup_client, monkeypatch, enabled):
    client, store = signup_client
    monkeypatch.setenv("SIGNUP_ENABLED", str(enabled))
    get_settings.cache_clear()

    configuration = await client.get("/site-config")
    response = await client.post("/auth/signup", json=create_signup_request())

    assert response.status_code == (201 if enabled else 403)
    assert configuration.json()["features"]["public_signup"] is enabled
    assert store.create_user.await_count == int(enabled)


@pytest.mark.asyncio
async def test_runtime_toggle_updates_both_endpoints_without_restarting(signup_client):
    client, store = signup_client
    runtime_settings = init_runtime_settings(store)

    assert (await client.get("/site-config")).json()["features"]["public_signup"] is True
    store.get_setting.return_value = {"value": "false", "value_type": "bool"}
    runtime_settings.invalidate_key("signup_enabled")

    assert (await client.get("/site-config")).json()["features"]["public_signup"] is False
    assert (await client.post("/auth/signup", json=create_signup_request())).status_code == 403
    store.create_user.assert_not_awaited()

    store.get_setting.return_value = None
    runtime_settings.invalidate_key("signup_enabled")
    assert (await client.get("/site-config")).json()["features"]["public_signup"] is True
    assert (await client.post("/auth/signup", json=create_signup_request())).status_code == 201
    store.create_user.assert_awaited_once()
