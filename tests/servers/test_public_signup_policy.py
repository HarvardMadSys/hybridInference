"""The signup API and public configuration must expose the same policy."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.auth.signup_policy import (
    invalidate_allowlist_cache,
    invalidate_signup_policy_cache,
)
from serving.config.distribution import get_distribution_config
from serving.config.runtime_settings import init_runtime_settings
from serving.config.settings import get_settings
from serving.servers.deps import get_db_logger, get_log_store, get_operational_store
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
    invalidate_signup_policy_cache()
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
    unavailable_log_store = AsyncMock()
    unavailable_log_store.account_has_erasure_fence.side_effect = AssertionError(
        "signup must not perform a post-commit LogStore fence lookup"
    )
    app.dependency_overrides[get_log_store] = lambda: unavailable_log_store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, store
    invalidate_allowlist_cache()
    invalidate_signup_policy_cache()


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


@pytest.mark.asyncio
async def test_policy_invalidation_discards_an_inflight_stale_value(signup_client):
    client, store = signup_client
    runtime_settings = init_runtime_settings(store)
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    read_count = 0

    async def read_setting(_key):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            read_started.set()
            await release_read.wait()
            return {"value": "true", "value_type": "bool"}
        return {"value": "false", "value_type": "bool"}

    store.get_setting.side_effect = read_setting
    pending = asyncio.create_task(client.get("/site-config"))
    await read_started.wait()

    # This is the synchronous invalidation sequence the admin handler runs
    # after persisting a new value.
    runtime_settings.invalidate_key("signup_enabled")
    invalidate_signup_policy_cache()
    release_read.set()

    configuration = await pending
    assert configuration.json()["features"]["public_signup"] is False
    assert read_count == 2
    assert (await client.post("/auth/signup", json=create_signup_request())).status_code == 403
    store.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_invalidation_discards_an_inflight_failure(signup_client):
    client, store = signup_client
    runtime_settings = init_runtime_settings(store)
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    read_count = 0

    async def read_setting(_key):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            read_started.set()
            await release_read.wait()
            raise ConnectionError("stale failure")
        return {"value": "true", "value_type": "bool"}

    store.get_setting.side_effect = read_setting
    pending = asyncio.create_task(client.get("/site-config"))
    await read_started.wait()

    runtime_settings.invalidate_key("signup_enabled")
    invalidate_signup_policy_cache()
    release_read.set()

    configuration = await pending
    assert configuration.json()["features"]["public_signup"] is True
    assert read_count == 2
    # The stale failure must not recreate the five-second fail-closed window.
    assert (await client.get("/site-config")).json()["features"]["public_signup"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["", "activ", "active"])
async def test_invalid_distribution_selection_cannot_enable_signup(
    signup_client, monkeypatch, tmp_path, mode
):
    client, store = signup_client
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", mode)
    if mode != "active":
        manifest = tmp_path / "distribution.yaml"
        manifest.write_text(
            "schema_version: 1\ndistribution: {id: example}\nfeatures: {public_signup: false}\n"
        )
        monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    get_settings.cache_clear()

    response = await client.post("/auth/signup", json=create_signup_request())
    assert response.status_code == 403
    assert (await client.get("/site-config")).status_code == 503
    store.create_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contents",
    [
        None,
        "not: [valid yaml",
        "schema_version: 1\ndistribution: {id: example}\nfeatures: {public-signup: false}\n",
        "schema_version: 1\ndistribution: {id: example}\nfeatures: {publicSignup: false}\n",
        "schema_version: 1\ndistribution: {id: example}\npublic_signup: false\n",
    ],
)
async def test_invalid_active_manifest_cannot_create_accounts(
    signup_client, monkeypatch, tmp_path, contents
):
    client, store = signup_client
    manifest = tmp_path / "private-manifest.yaml"
    if contents is not None:
        manifest.write_text(contents)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    get_settings.cache_clear()

    response = await client.post("/auth/signup", json=create_signup_request())
    assert response.status_code == 403
    response = await client.get("/site-config")
    assert response.status_code == 503
    assert "private-manifest" not in response.text
    store.create_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "dark", "active"])
@pytest.mark.parametrize("failure", [ConnectionError, RuntimeError, ValueError])
async def test_runtime_read_failure_preserves_identity_and_closes_signup(
    signup_client, monkeypatch, tmp_path, caplog, mode, failure
):
    client, store = signup_client
    if mode is not None:
        manifest = tmp_path / "distribution.yaml"
        manifest.write_text(
            "schema_version: 1\ndistribution: {id: example, display_name: Example Router}\n"
            "site: {public_base_url: 'https://example.test'}\n"
            "features: {public_signup: true, rag: false}\n"
        )
        monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
        monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", mode)
    get_settings.cache_clear()
    runtime = init_runtime_settings(store)
    await runtime.get_bool("signup_enabled")
    # A previously enabled value must not survive an expired policy read.
    runtime.invalidate_key("signup_enabled")
    store.get_setting.side_effect = failure("private-database secret-canary")

    configuration = await client.get("/site-config")
    assert configuration.status_code == 200
    body = configuration.json()
    assert body["features"]["public_signup"] is False
    if mode == "active":
        assert body["distribution"]["display_name"] == "Example Router"
        assert body["site"]["public_base_url"] == "https://example.test"
        assert body["features"]["rag"] is False
    else:
        assert body["distribution"]["id"] == "neutral"
    assert (await client.post("/auth/signup", json=create_signup_request())).status_code == 403
    store.create_user.assert_not_awaited()
    assert "public signup is disabled" in caplog.text
    assert "secret-canary" not in caplog.text
    assert "private-database" not in caplog.text

    store.get_setting.side_effect = None
    # The fail-closed window stands until it expires or an administrator
    # writes the setting; a recovered store does not reopen signup mid-window.
    assert (await client.get("/site-config")).json()["features"]["public_signup"] is False
    invalidate_signup_policy_cache()
    assert (await client.get("/site-config")).json()["features"]["public_signup"] is True


@pytest.mark.asyncio
async def test_hung_runtime_read_cannot_exhaust_console_configuration_deadline(signup_client):
    client, store = signup_client
    cancelled = asyncio.Event()

    async def stuck_read(key):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    store.get_setting.side_effect = stuck_read
    init_runtime_settings(store)
    response = await asyncio.wait_for(client.get("/site-config"), timeout=2)

    assert response.status_code == 200
    assert response.json()["features"]["public_signup"] is False
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_concurrent_configuration_reads_share_one_policy_lookup(signup_client):
    """An expired policy TTL costs one store round-trip, not one per request."""
    client, store = signup_client
    reads = 0

    async def slow_read(key):
        nonlocal reads
        reads += 1
        await asyncio.sleep(0.05)
        return None

    store.get_setting.side_effect = slow_read
    init_runtime_settings(store)

    responses = await asyncio.gather(*(client.get("/site-config") for _ in range(8)))

    assert [r.json()["features"]["public_signup"] for r in responses] == [True] * 8
    assert reads == 1


@pytest.mark.asyncio
async def test_failed_policy_read_is_not_repeated_for_every_request(signup_client):
    """A store that is down costs one timeout per window, not one per request."""
    client, store = signup_client
    store.get_setting.side_effect = ConnectionError("store is down")
    init_runtime_settings(store)

    for _ in range(5):
        response = await client.get("/site-config")
        assert response.status_code == 200
        assert response.json()["features"]["public_signup"] is False

    assert (await client.post("/auth/signup", json=create_signup_request())).status_code == 403
    assert store.get_setting.await_count == 1
    store.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_hung_policy_read_stalls_only_the_first_window(signup_client):
    """Requests behind a hung store return promptly instead of queueing timeouts."""
    client, store = signup_client

    async def stuck_read(key):
        await asyncio.Event().wait()

    store.get_setting.side_effect = stuck_read
    init_runtime_settings(store)

    await asyncio.wait_for(client.get("/site-config"), timeout=2)
    started = asyncio.get_running_loop().time()
    for _ in range(3):
        response = await asyncio.wait_for(client.get("/site-config"), timeout=2)
        assert response.json()["features"]["public_signup"] is False
    # Three more requests must not cost three more one-second timeouts.
    assert asyncio.get_running_loop().time() - started < 1.0
    assert store.get_setting.await_count == 1
