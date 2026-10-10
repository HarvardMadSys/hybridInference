"""PostgreSQL integration tests for the ``app_config`` store and the runtime import."""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from serving.config.app_config_store import AppConfigStore, ConfigRow
from serving.config.runtime_settings import import_environment_values

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn


@pytest_asyncio.fixture
async def pool(pg_dsn: str):
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    async with pool.acquire() as connection:
        await connection.execute("DROP TABLE IF EXISTS app_config")
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS site_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                value_type TEXT NOT NULL DEFAULT 'str',
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                updated_by TEXT
            )
            """
        )
        await connection.execute(
            "DELETE FROM site_settings WHERE key IN ('user_auth_enabled', 'signup_enabled')"
        )
    try:
        yield pool
    finally:
        async with pool.acquire() as connection:
            await connection.execute("DROP TABLE IF EXISTS app_config")
            await connection.execute(
                "DELETE FROM site_settings WHERE key IN ('user_auth_enabled', 'signup_enabled')"
            )
        await pool.close()


@pytest.mark.asyncio
async def test_store_round_trip_on_a_pool_and_on_a_connection(pool, pg_dsn) -> None:
    store = AppConfigStore(pool)
    await store.ensure_schema()
    await store.ensure_schema()  # idempotent

    added = await store.insert_missing(
        [
            ConfigRow("SITE_NAME", "Imported", source="env_import", updated_by="env-import"),
            ConfigRow("SMTP_PASSWORD", "pw", secret=True, source="env_import"),
        ]
    )
    assert sorted(added) == ["SITE_NAME", "SMTP_PASSWORD"]
    # Existing rows are never overwritten by an import.
    assert await store.insert_missing([ConfigRow("SITE_NAME", "Other", source="env_import")]) == []

    await store.write(
        [
            ConfigRow("SITE_NAME", "By admin", updated_by="admin@example.com"),
            ConfigRow("BOX_TOKEN", "t", secret=True, updated_by="admin@example.com"),
        ]
    )
    rows = await store.fetch_all()
    assert rows["SITE_NAME"].value == "By admin"
    assert rows["SITE_NAME"].source == "admin"
    assert rows["SITE_NAME"].updated_by == "admin@example.com"
    assert rows["SITE_NAME"].updated_at is not None
    assert rows["SMTP_PASSWORD"].secret is True
    assert rows["BOX_TOKEN"].secret is True

    assert await store.delete("BOX_TOKEN") is True
    assert await store.delete("BOX_TOKEN") is False

    connection = await asyncpg.connect(pg_dsn)
    try:
        single = AppConfigStore(connection)
        assert set(await single.fetch_all()) == {"SITE_NAME", "SMTP_PASSWORD"}
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_api_keys_exist_checks_the_table(pool) -> None:
    store = AppConfigStore(pool)
    async with pool.acquire() as connection:
        exists = await connection.fetchval("SELECT to_regclass('api_keys') IS NOT NULL")
        if exists:
            has_rows = await connection.fetchval("SELECT EXISTS (SELECT 1 FROM api_keys)")
        else:
            has_rows = False
    assert await store.api_keys_exist() is bool(has_rows)


@pytest.mark.asyncio
async def test_runtime_settings_import_inserts_only_missing_rows(pool, monkeypatch) -> None:
    from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY

    for key in RUNTIME_SETTINGS_REGISTRY:
        monkeypatch.delenv(key.upper(), raising=False)
    for key in ("USER_AUTH_ENABLED", "SIGNUP_ENABLED"):
        monkeypatch.setenv(key, "on")
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO site_settings (key, value, value_type, updated_by) "
            "VALUES ('signup_enabled', 'False', 'bool', 'admin')"
        )

    imported = await import_environment_values(pool)

    assert "user_auth_enabled" in imported
    assert "signup_enabled" not in imported
    async with pool.acquire() as connection:
        rows = {
            record["key"]: record
            for record in await connection.fetch(
                "SELECT key, value, value_type, updated_by FROM site_settings "
                "WHERE key IN ('user_auth_enabled', 'signup_enabled')"
            )
        }
    assert rows["user_auth_enabled"]["value"] == "True"
    assert rows["user_auth_enabled"]["updated_by"] == "env-import"
    assert rows["signup_enabled"]["value"] == "False"
    assert imported == ["user_auth_enabled"]
    # Twice is the same as once.
    assert await import_environment_values(pool) == []


@pytest_asyncio.fixture
async def isolated_connection(pg_dsn: str):
    """A connection whose search_path is a fresh schema, so ``api_keys`` is ours alone."""
    import secrets

    schema = f"app_config_it_{secrets.token_hex(4)}"
    admin = await asyncpg.connect(pg_dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    connection = await asyncpg.connect(pg_dsn, server_settings={"search_path": schema})
    try:
        yield connection
    finally:
        await connection.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_boot_load_imports_generates_and_guards_api_keys(
    isolated_connection, monkeypatch, tmp_path
) -> None:
    from serving.config import app_config
    from serving.config.app_config_registry import static_entries
    from serving.config.settings import get_settings

    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("SITE_NAME", "Imported")
    get_settings.cache_clear()
    store = AppConfigStore(isolated_connection)

    await app_config.load(store)

    rows = await store.fetch_all()
    assert rows["SITE_NAME"].source == "env_import"
    assert rows["JWT_SECRET_KEY"].source == "generated"
    assert rows["API_KEY_SECRET"].source == "generated"
    assert get_settings().api_key_secret == rows["API_KEY_SECRET"].value

    # Lose the secret while keys hashed with it exist: startup must refuse.
    await isolated_connection.execute("DELETE FROM app_config WHERE key = 'API_KEY_SECRET'")
    await isolated_connection.execute("CREATE TABLE api_keys (id BIGSERIAL PRIMARY KEY)")
    await isolated_connection.execute("INSERT INTO api_keys DEFAULT VALUES")
    app_config.reset_state()

    with pytest.raises(app_config.ConfigBootstrapError, match="API_KEY_SECRET"):
        await app_config.load(store)
    assert "API_KEY_SECRET" not in await store.fetch_all()
