"""Bootstrap loads the database-backed configuration before anything reads it."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.config import app_config
from serving.config.settings import get_settings
from serving.servers import bootstrap
from tests.fixtures.app_config_store import FakeAppConfigStore, row


@pytest.fixture
def boot_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "env-jwt")
    monkeypatch.setenv("API_KEY_SECRET", "env-api")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.delenv("DB_STORE_FULL_CONTENT", raising=False)
    monkeypatch.setattr(bootstrap, "_CONFIG_LOAD_RETRY_DELAY", 0)
    get_settings.cache_clear()


def _connection() -> MagicMock:
    connection = MagicMock()
    connection.close = AsyncMock()
    return connection


@pytest.mark.asyncio
async def test_config_load_retries_then_uses_the_database(boot_env, monkeypatch) -> None:
    store = FakeAppConfigStore([row("SITE_NAME", "Stored")])
    connection = _connection()
    connect = AsyncMock(side_effect=[OSError("down"), OSError("down"), connection])
    monkeypatch.setattr(bootstrap, "connect_app_config", connect)
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: store)

    await bootstrap._load_app_config()

    assert connect.await_count == 3
    connection.close.assert_awaited_once()
    assert app_config.config_value("SITE_NAME") == "Stored"


@pytest.mark.asyncio
async def test_an_unreachable_database_leaves_the_environment_in_charge(
    boot_env, monkeypatch
) -> None:
    monkeypatch.setenv("SITE_NAME", "From env")
    connect = AsyncMock(side_effect=OSError("down"))
    monkeypatch.setattr(bootstrap, "connect_app_config", connect)

    await bootstrap._load_app_config()

    assert connect.await_count == bootstrap._CONFIG_LOAD_ATTEMPTS
    assert app_config.config_value("SITE_NAME") == "From env"
    # The health check still runs, on the environment.
    assert app_config.get_config_health() is not None


@pytest.mark.asyncio
async def test_a_refused_generation_stops_startup_without_retrying(boot_env, monkeypatch) -> None:
    monkeypatch.delenv("API_KEY_SECRET")
    connection = _connection()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(bootstrap, "connect_app_config", connect)
    monkeypatch.setattr(
        bootstrap, "AppConfigStore", lambda conn: FakeAppConfigStore(api_keys_exist=True)
    )

    with pytest.raises(app_config.ConfigBootstrapError):
        await bootstrap._load_app_config()

    assert connect.await_count == 1
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_database_free_gateway_never_connects(boot_env, monkeypatch) -> None:
    monkeypatch.setenv("DB_ENABLED", "false")
    connect = AsyncMock()
    monkeypatch.setattr(bootstrap, "connect_app_config", connect)

    await bootstrap._load_app_config()

    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_database_logger_is_built_from_stored_values(boot_env, monkeypatch) -> None:
    """DB_STORE_FULL_CONTENT and the fence secret reach the logger from the database."""
    store = FakeAppConfigStore(
        [
            row("DB_STORE_FULL_CONTENT", "true"),
            row("ERASURE_FENCE_SECRET", "stored-fence-secret", secret=True),
        ]
    )
    monkeypatch.setattr(bootstrap, "connect_app_config", AsyncMock(return_value=_connection()))
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: store)
    # Record the constructor call, then fail it: the gateway runs on without a logger.
    database_logger = MagicMock(side_effect=RuntimeError("not in this test"))
    monkeypatch.setattr(bootstrap, "DatabaseLogger", database_logger)
    monkeypatch.setattr(bootstrap, "_init_router_and_models", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(bootstrap, "_apply_routing_manager", MagicMock(return_value=None))

    services = await bootstrap.initialize()
    await bootstrap.shutdown(services)

    kwargs = database_logger.call_args.kwargs
    assert kwargs["store_full_prompts"] is True
    assert kwargs["fence_secret"] == "stored-fence-secret"


@pytest.mark.asyncio
async def test_boot_wires_runtime_import_setup_state_and_refresh_loops(
    boot_env, monkeypatch
) -> None:
    store = FakeAppConfigStore()
    monkeypatch.setattr(bootstrap, "connect_app_config", AsyncMock(return_value=_connection()))
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: store)

    db_logger = AsyncMock()
    db_logger.pool = MagicMock()
    monkeypatch.setattr(bootstrap, "_init_db_logger", MagicMock(return_value=db_logger))
    monkeypatch.setattr(bootstrap, "_init_router_and_models", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(bootstrap, "_apply_routing_manager", MagicMock(return_value=None))
    monkeypatch.setattr(bootstrap, "PostgresOperationalStore", MagicMock(return_value=AsyncMock()))
    operational_store = MagicMock()
    operational_store.list_settings = AsyncMock(return_value=[])
    monkeypatch.setattr(
        bootstrap, "CachedOperationalStore", MagicMock(return_value=operational_store)
    )
    monkeypatch.setattr(bootstrap, "ResponseStore", MagicMock(return_value=AsyncMock()))
    import_environment_values = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "serving.config.runtime_settings.import_environment_values", import_environment_values
    )
    init_setup_state = AsyncMock()
    monkeypatch.setattr(bootstrap, "init_setup_state", init_setup_state)

    services = await bootstrap.initialize()
    try:
        import_environment_values.assert_awaited_once_with(db_logger.pool)
        init_setup_state.assert_awaited_once_with(operational_store)
        operational_store.list_settings.assert_awaited()  # one-query warmup
        assert app_config._STATE.store is not None
        assert app_config._STATE.refresh_task is not None
        assert services.runtime_settings._refresh_task is not None
    finally:
        await bootstrap.shutdown(services)

    assert app_config._STATE.refresh_task is None
    assert services.runtime_settings._refresh_task is None


@pytest.mark.asyncio
async def test_database_free_boot_still_initializes_setup_state(boot_env, monkeypatch) -> None:
    monkeypatch.setenv("DB_ENABLED", "false")
    monkeypatch.setattr(bootstrap, "_init_db_logger", MagicMock(return_value=None))
    monkeypatch.setattr(bootstrap, "_init_router_and_models", AsyncMock(return_value=({}, [])))
    monkeypatch.setattr(bootstrap, "_apply_routing_manager", MagicMock(return_value=None))
    init_setup_state = AsyncMock()
    monkeypatch.setattr(bootstrap, "init_setup_state", init_setup_state)

    services = await bootstrap.initialize()
    await bootstrap.shutdown(services)

    init_setup_state.assert_awaited_once_with(None)
    assert app_config._STATE.refresh_task is None


# --- an environment value that is not valid ---------------------------------


@pytest.mark.asyncio
async def test_a_valid_stored_value_lets_startup_past_an_invalid_environment_value(
    boot_env, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("TRUSTED_PROXIES", "not-a-cidr")
    get_settings.cache_clear()
    store = FakeAppConfigStore([row("TRUSTED_PROXIES", "10.0.0.0/8")])
    monkeypatch.setattr(bootstrap, "connect_app_config", AsyncMock(return_value=_connection()))
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: store)

    await bootstrap._load_app_config()
    with caplog.at_level("WARNING", logger="serving.config.app_config"):
        app_config.require_valid_environment()

    assert get_settings().trusted_proxies == "10.0.0.0/8"
    assert [str(network) for network in get_settings().trusted_proxies_parsed] == ["10.0.0.0/8"]
    assert "Ignoring TRUSTED_PROXIES from the environment" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored,reachable,remedy",
    [
        ([], True, "manage set TRUSTED_PROXIES VALUE`, then"),
        ([row("TRUSTED_PROXIES", "also-not-a-cidr")], True, "manage set TRUSTED_PROXIES"),
        ([], False, "once the database can be reached"),
    ],
)
async def test_startup_refuses_an_invalid_environment_value_nothing_replaces(
    boot_env, monkeypatch, stored, reachable, remedy
) -> None:
    monkeypatch.setenv("TRUSTED_PROXIES", "not-a-cidr")
    get_settings.cache_clear()
    store = FakeAppConfigStore(stored)
    connect = AsyncMock(return_value=_connection()) if reachable else AsyncMock(side_effect=OSError)
    monkeypatch.setattr(bootstrap, "connect_app_config", connect)
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: store)

    await bootstrap._load_app_config()
    with pytest.raises(app_config.ConfigBootstrapError) as excinfo:
        app_config.require_valid_environment()

    assert "TRUSTED_PROXIES: trusted_proxies entry 'not-a-cidr'" in str(excinfo.value)
    assert remedy in str(excinfo.value)
    # Never imported: stored, it would outlive a corrected environment.
    assert store.rows.get("TRUSTED_PROXIES") == (stored[0] if stored else None)


def _cloudflare_environment(monkeypatch) -> None:
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "true")
    monkeypatch.setenv("TRUSTED_CLOUDFLARE_NETWORKS", "not-a-cidr")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_stored_value_a_rule_depends_on_lets_startup_past_the_environment(
    boot_env, monkeypatch, caplog
) -> None:
    """Cloudflare trust needs its networks; the stored ones apply before the rule is judged."""
    _cloudflare_environment(monkeypatch)
    store = FakeAppConfigStore([row("TRUSTED_CLOUDFLARE_NETWORKS", "173.245.48.0/20")])
    monkeypatch.setattr(bootstrap, "connect_app_config", AsyncMock(return_value=_connection()))
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: store)

    await bootstrap._load_app_config()
    with caplog.at_level("WARNING", logger="serving.config.app_config"):
        app_config.require_valid_environment()

    settings = get_settings()
    assert settings.trust_cloudflare_headers is True
    assert settings.trusted_cloudflare_networks == "173.245.48.0/20"
    assert [str(network) for network in settings.trusted_cloudflare_parsed] == ["173.245.48.0/20"]
    assert "Ignoring TRUSTED_CLOUDFLARE_NETWORKS from the environment" in caplog.text


@pytest.mark.asyncio
async def test_startup_refuses_a_rule_no_stored_value_satisfies(boot_env, monkeypatch) -> None:
    _cloudflare_environment(monkeypatch)
    store = FakeAppConfigStore([row("SITE_NAME", "Stored")])
    monkeypatch.setattr(bootstrap, "connect_app_config", AsyncMock(return_value=_connection()))
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: store)

    await bootstrap._load_app_config()
    with pytest.raises(app_config.ConfigBootstrapError) as excinfo:
        app_config.require_valid_environment()

    assert "TRUSTED_CLOUDFLARE_NETWORKS: trusted_cloudflare_networks entry 'not-a-cidr'" in str(
        excinfo.value
    )
    assert "manage set TRUSTED_CLOUDFLARE_NETWORKS VALUE" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_stored_value_satisfies_a_rule_the_environment_alone_breaks(
    boot_env, monkeypatch
) -> None:
    """TRUST_PROXY_HEADERS was imported, then removed from the environment."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setenv("TRUST_X_REAL_IP", "true")
    get_settings.cache_clear()
    store = FakeAppConfigStore([row("TRUST_PROXY_HEADERS", "true"), row("TRUST_X_REAL_IP", "true")])
    monkeypatch.setattr(bootstrap, "connect_app_config", AsyncMock(return_value=_connection()))
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: store)

    await bootstrap._load_app_config()
    app_config.require_valid_environment()

    assert get_settings().trust_proxy_headers is True
    assert get_settings().trust_x_real_ip is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [[], [row("TRUST_PROXY_HEADERS", "false")]],
    ids=["nothing stored", "stored value breaks it too"],
)
async def test_startup_refuses_a_rule_the_configuration_in_force_breaks(
    boot_env, monkeypatch, stored
) -> None:
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setenv("TRUST_X_REAL_IP", "true")
    get_settings.cache_clear()
    store = FakeAppConfigStore(stored)
    monkeypatch.setattr(bootstrap, "connect_app_config", AsyncMock(return_value=_connection()))
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: store)

    await bootstrap._load_app_config()
    with pytest.raises(app_config.ConfigBootstrapError) as excinfo:
        app_config.require_valid_environment()

    message = str(excinfo.value)
    assert "TRUST_X_REAL_IP requires TRUST_PROXY_HEADERS" in message
    assert "Correct TRUST_PROXY_HEADERS, TRUST_X_REAL_IP" in message
    assert "manage set" in message
    # Neither side was imported: which one is wrong is the operator's call.
    assert "TRUST_X_REAL_IP" not in store.rows


@pytest.mark.asyncio
async def test_a_database_free_gateway_refuses_a_broken_rule(boot_env, monkeypatch) -> None:
    monkeypatch.setenv("DB_ENABLED", "false")
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "1")
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    get_settings.cache_clear()

    await bootstrap._load_app_config()
    with pytest.raises(app_config.ConfigBootstrapError) as excinfo:
        app_config.require_valid_environment()

    assert "TRUST_CLOUDFLARE_HEADERS requires TRUST_PROXY_HEADERS" in str(excinfo.value)
    assert "manage" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_database_free_gateway_refuses_an_invalid_environment_value(
    boot_env, monkeypatch
) -> None:
    monkeypatch.setenv("DB_ENABLED", "false")
    monkeypatch.setenv("TRUSTED_PROXIES", "not-a-cidr")
    get_settings.cache_clear()

    await bootstrap._load_app_config()
    with pytest.raises(app_config.ConfigBootstrapError, match="Fix or remove the variable, then"):
        app_config.require_valid_environment()


@pytest.mark.asyncio
async def test_a_runtime_switch_is_not_offered_to_the_manage_command(boot_env, monkeypatch) -> None:
    """USER_AUTH_ENABLED lives on the Settings tab: the command would refuse it."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "maybe")
    get_settings.cache_clear()
    monkeypatch.setattr(bootstrap, "connect_app_config", AsyncMock(return_value=_connection()))
    monkeypatch.setattr(bootstrap, "AppConfigStore", lambda conn: FakeAppConfigStore())

    await bootstrap._load_app_config()
    with pytest.raises(app_config.ConfigBootstrapError) as excinfo:
        app_config.require_valid_environment()

    assert "USER_AUTH_ENABLED:" in str(excinfo.value)
    assert "manage" not in str(excinfo.value)


def test_startup_checks_the_environment_right_after_loading_the_configuration() -> None:
    """Pins the wiring: before the auth secrets, and before anything else starts."""
    import inspect

    source = inspect.getsource(bootstrap.initialize)
    load = source.index("await _load_app_config()")
    check = source.index("app_config.require_valid_environment()")
    secrets = source.index("validate_auth_secrets")
    assert load < check < secrets
    assert source[load:check].count("\n") <= 4
