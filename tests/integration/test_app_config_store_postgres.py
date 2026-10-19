"""PostgreSQL integration tests for the ``app_config`` store and the runtime import."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from serving.config.app_config_store import AppConfigStore, ConfigRow
from serving.config.runtime_settings import import_environment_values
from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_operation", ["save", "reset"])
async def test_concurrent_config_changes_validate_the_previous_commit(
    pool, pg_dsn, monkeypatch, tmp_path, first_operation
) -> None:
    """Two database connections must not approve an invalid combined change."""
    from serving.config import app_config
    from serving.config.app_config_registry import static_entries
    from serving.config.settings import get_settings

    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("TRUST_X_REAL_IP", "false")
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "false")
    for name in ("MODELS_CONFIG_PATH", "ROUTING_CONFIG_PATH", "ALERTS_CONFIG_PATH"):
        monkeypatch.setenv(name, str(tmp_path / name))
    get_settings.cache_clear()
    store = AppConfigStore(pool)
    await store.ensure_schema()
    await store.write(
        [
            ConfigRow("TRUST_PROXY_HEADERS", "true"),
            ConfigRow("TRUST_X_REAL_IP", "false"),
        ]
    )
    await app_config.load(store, bootstrap=False)
    app_config.attach(store)

    # Hold the first write after validation. The second request can now either
    # wait for it or (without serialization) validate the same stale snapshot.
    blocker = await postgres.connect()
    transaction = blocker.transaction()
    await transaction.start()
    tasks = []
    released = False

    async def wait_for_write(*, second=None):
        async def waiting():
            while True:
                await blocker.execute("SELECT pg_stat_clear_snapshot()")
                waiters = await blocker.fetch(
                    "SELECT query, wait_event FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
                if second is None:
                    if any("INSERT INTO app_config" in row["query"] for row in waiters):
                        return
                elif second.done() or any(row["wait_event"] == "advisory" for row in waiters):
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(waiting(), timeout=5)

    try:
        await blocker.fetchrow(
            "SELECT key FROM app_config WHERE key = 'TRUST_PROXY_HEADERS' FOR UPDATE"
        )
        first = asyncio.create_task(
            app_config.update({"TRUST_PROXY_HEADERS": False}, updated_by="first")
            if first_operation == "save"
            else app_config.delete("TRUST_PROXY_HEADERS", updated_by="first")
        )
        tasks.append(first)
        await wait_for_write()
        second = asyncio.create_task(
            app_config.update({"TRUST_X_REAL_IP": True}, updated_by="second")
        )
        tasks.append(second)
        await wait_for_write(second=second)
        await transaction.commit()
        released = True
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=5
        )
        assert not isinstance(results[0], Exception), results[0]
        assert isinstance(results[1], app_config.ConfigUpdateError), results
        assert results[1].status_code == 400
        assert "TRUST_PROXY_HEADERS" in results[1].detail
        rows = await store.fetch_all()
        assert rows["TRUST_X_REAL_IP"].value == "false"
        assert not get_settings().trust_proxy_headers
        assert not get_settings().trust_x_real_ip
    finally:
        if not released:
            await transaction.rollback()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await blocker.close()


@pytest.mark.asyncio
async def test_failed_config_transaction_rolls_back_and_releases_the_lock(pool) -> None:
    store = AppConfigStore(pool)
    await store.ensure_schema()
    with pytest.raises(ValueError, match="abort"):
        async with store.transaction() as transaction:
            await transaction.write([ConfigRow("SITE_NAME", "rolled back")])
            raise ValueError("abort")
    assert await store.fetch_all() == {}
    async with store.transaction() as transaction:
        await transaction.write([ConfigRow("SITE_NAME", "committed")])
    assert (await store.fetch_all())["SITE_NAME"].value == "committed"


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """This worker's test database (tests/fixtures/postgres.py), reachable or skipped."""
    return postgres.worker_dsn()


@pytest_asyncio.fixture
async def pool(pg_dsn: str):
    pool = await postgres.create_pool(min_size=1, max_size=2)
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

    connection = await postgres.connect()
    try:
        single = AppConfigStore(connection)
        assert set(await single.fetch_all()) == {"SITE_NAME", "SMTP_PASSWORD"}
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_a_reset_row_stores_no_value_and_keeps_the_import_out(pool) -> None:
    store = AppConfigStore(pool)
    await store.ensure_schema()
    await store.insert_missing(
        [
            ConfigRow("SITE_NAME", "Imported", source="env_import"),
            ConfigRow("JWT_SECRET_KEY", "from-env", secret=True, source="env_import"),
        ]
    )

    await store.reset("SITE_NAME", secret=False, updated_by="admin@example.com")
    await store.reset("JWT_SECRET_KEY", secret=True, updated_by="admin@example.com")
    await store.reset("SITE_NAME", secret=False, updated_by="admin@example.com")  # idempotent
    assert await store.fetch_all() == {}

    # The boot-time import leaves a reset key alone, so it keeps following the
    # environment...
    assert await store.insert_missing([ConfigRow("SITE_NAME", "Again", source="env_import")]) == []
    assert await store.fetch_all() == {}
    # ...a secret that has to be generated replaces the reset row...
    generated = ConfigRow("JWT_SECRET_KEY", "generated", secret=True, source="generated")
    assert await store.insert_missing([generated], replace_reset=True) == ["JWT_SECRET_KEY"]
    # ...but never a stored value.
    newer = ConfigRow("JWT_SECRET_KEY", "newer", secret=True, source="generated")
    assert await store.insert_missing([newer], replace_reset=True) == []
    assert (await store.fetch_all())["JWT_SECRET_KEY"].value == "generated"

    # An administrator's value replaces a reset row.
    await store.write([ConfigRow("SITE_NAME", "By admin", updated_by="admin@example.com")])
    assert (await store.fetch_all())["SITE_NAME"].value == "By admin"


@pytest.mark.asyncio
async def test_reset_rows_are_returned_on_request_with_their_secret_flag(pool) -> None:
    store = AppConfigStore(pool)
    await store.ensure_schema()
    await store.write([ConfigRow("SITE_NAME", "Stored"), ConfigRow("BOX_URL", "u", secret=True)])
    await store.reset("BOX_URL", secret=True, updated_by="admin@example.com")

    assert set(await store.fetch_all()) == {"SITE_NAME"}
    everything = await store.fetch_all(include_reset=True)
    assert set(everything) == {"SITE_NAME", "BOX_URL"}
    assert everything["BOX_URL"].source == "reset"
    assert everything["BOX_URL"].value == ""
    assert everything["BOX_URL"].secret is True


@pytest.mark.asyncio
async def test_the_secret_flag_is_one_way(pool) -> None:
    store = AppConfigStore(pool)
    await store.ensure_schema()
    await store.write(
        [
            ConfigRow("BOX_REGION", "east", updated_by="admin@example.com"),
            ConfigRow("BOX_URL", "u", source="env_import", updated_by="env-import"),
        ]
    )
    await store.reset("BOX_URL", secret=False, updated_by="admin@example.com")
    before = (await store.fetch_all(include_reset=True))["BOX_REGION"]

    assert sorted(await store.mark_secret(["BOX_REGION", "BOX_URL", "NOT_STORED"])) == [
        "BOX_REGION",
        "BOX_URL",
    ]
    assert await store.mark_secret(["BOX_REGION"]) == []  # already secret
    assert await store.mark_secret([]) == []
    rows = await store.fetch_all(include_reset=True)
    assert rows["BOX_REGION"].secret and rows["BOX_URL"].secret
    # Only the flag: the value and who last set it are unchanged.
    assert (rows["BOX_REGION"].value, rows["BOX_REGION"].updated_by) == (
        "east",
        "admin@example.com",
    )
    assert rows["BOX_REGION"].updated_at == before.updated_at
    assert rows["BOX_URL"].source == "reset"

    # Neither a write nor a reset clears it.
    await store.write([ConfigRow("BOX_REGION", "west", secret=False)])
    await store.reset("BOX_URL", secret=False, updated_by="admin@example.com")
    await store.write([ConfigRow("BOX_URL", "v", secret=False)])
    rows = await store.fetch_all(include_reset=True)
    assert rows["BOX_REGION"].value == "west" and rows["BOX_REGION"].secret
    assert rows["BOX_URL"].value == "v" and rows["BOX_URL"].secret


@pytest.mark.asyncio
async def test_concurrent_workers_replace_a_reset_secret_once(pool) -> None:
    store = AppConfigStore(pool)
    await store.ensure_schema()
    await store.reset("JWT_SECRET_KEY", secret=True, updated_by="admin@example.com")

    results = await asyncio.gather(
        *(
            store.insert_missing(
                [ConfigRow("JWT_SECRET_KEY", value, secret=True, source="generated")],
                replace_reset=True,
            )
            for value in ("worker-a", "worker-b")
        )
    )

    assert sorted(len(added) for added in results) == [0, 1]
    winner = "worker-a" if results[0] else "worker-b"
    assert (await store.fetch_all())["JWT_SECRET_KEY"].value == winner


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
    admin = await postgres.connect()
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    connection = await postgres.connect(server_settings={"search_path": schema})
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
