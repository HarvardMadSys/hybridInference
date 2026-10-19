"""Configuration ownership across real PostgreSQL transactions and fresh processes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import asyncpg
import pytest
import pytest_asyncio

from serving.config import app_config, app_config_registry as registry
from serving.config.app_config_store import AppConfigStore, ConfigOwnershipError, ConfigRow
from serving.config.settings import get_settings
from tests.conftest import subprocess_env
from tests.fixtures import postgres

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.dbtest, pytest.mark.asyncio]

OWNER = "extension:sample.delivery"
REGION = "SAMPLE_DELIVERY_REGION"
SECRET = "SAMPLE_DELIVERY_CREDENTIAL"
RESET = "SAMPLE_DELIVERY_RESET"
UNKNOWN = "UNCLAIMED_LEGACY_ENDPOINT"
STAMP = datetime(2026, 9, 1, 2, 3, 4, tzinfo=timezone.utc)

OLD_SCHEMA = """
CREATE TABLE app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    secret BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT NOT NULL DEFAULT 'admin',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT
)
"""


@pytest.fixture
def clean_environment(monkeypatch, tmp_path):
    """No deployment environment, installed extensions or model files enter a child."""
    for entry in registry.static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.setattr(registry, "_EXTENSION_ENTRIES", {})
    monkeypatch.setattr(registry, "_OWNERSHIP_MIGRATIONS", {})
    registry.invalidate_config_registry()
    for key in ("DISTRIBUTION_CONFIG_PATH", "ENVIRONMENT", REGION, SECRET, RESET, UNKNOWN):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BACKEND_EXTENSIONS", "")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    # A worker database may already contain users/API keys from another test.
    # This suite exercises ownership rather than regenerating their secrets.
    monkeypatch.setenv("API_KEY_SECRET", "ownership-test-api-key-secret")
    monkeypatch.setenv("JWT_SECRET_KEY", "ownership-test-jwt-secret")
    models = tmp_path / "models.yaml"
    models.write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(models))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    registry.invalidate_config_registry()


@pytest_asyncio.fixture
async def database(clean_environment):
    """Only this worker's test database; no operational tables are changed."""
    pool = await postgres.create_pool(min_size=1, max_size=4)
    async with pool.acquire() as connection:
        await connection.execute("DROP TABLE IF EXISTS app_config_ownership_migrations")
        await connection.execute("DROP TABLE IF EXISTS app_config")
        await connection.execute(OLD_SCHEMA)
    try:
        yield pool
    finally:
        async with pool.acquire() as connection:
            await connection.execute("DROP TABLE IF EXISTS app_config_ownership_migrations")
            await connection.execute("DROP TABLE IF EXISTS app_config")
        await pool.close()


async def legacy_rows(pool, *rows):
    async with pool.acquire() as connection:
        await connection.executemany(
            "INSERT INTO app_config (key, value, secret, source, updated_at, updated_by) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            [
                (key, value, secret, source, STAMP, "original-admin")
                for key, value, secret, source in rows
            ],
        )


async def raw_rows(pool):
    async with pool.acquire() as connection:
        rows = await connection.fetch("SELECT * FROM app_config ORDER BY key")
    return {row["key"]: dict(row) for row in rows}


def migration(
    identifier="sample-delivery-v1", *, keys=(REGION, SECRET, RESET), secret_keys=(SECRET,)
):
    return registry.ConfigOwnershipMigration(
        id=identifier, owner=OWNER, keys=keys, secret_keys=secret_keys
    )


def register_sample():
    registry.register_config_entries(
        [
            registry.ConfigEntry(REGION, "email", "Delivery region.", default="default-region"),
            registry.ConfigEntry(SECRET, "email", "Delivery credential.", secret=True),
            registry.ConfigEntry(RESET, "email", "Reset delivery setting."),
        ],
        owner=OWNER,
    )
    registry.register_config_ownership_migration(migration())


async def child(arguments, cwd: Path, **environment):
    env = subprocess_env(
        **postgres.target().app_env(),
        PYTHON_DOTENV_DISABLED="1",
        BACKEND_EXTENSIONS="",
    )
    env.update(environment)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *arguments,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return process.returncode, stdout.decode(), stderr.decode()


async def snapshot(cwd: Path, keys, **environment):
    script = textwrap.dedent(
        f"""
        import asyncio
        import json
        from serving.config import app_config

        async def main():
            async with app_config.command_line_session():
                body = app_config.describe()
                print('OWNERSHIP_SNAPSHOT=' + json.dumps({{
                    'resolved': {{key: app_config.config_value(key) for key in {keys!r}}},
                    'body': body,
                }}, default=str))

        asyncio.run(main())
        """
    )
    code, stdout, stderr = await child(["-c", script], cwd, **environment)
    assert code == 0, stderr
    lines = [line for line in stdout.splitlines() if line.startswith("OWNERSHIP_SNAPSHOT=")]
    assert len(lines) == 1, stdout
    return json.loads(lines[0].partition("=")[2])


async def test_unknown_legacy_rows_need_explicit_cli_adoption(database, clean_environment):
    await legacy_rows(database, (UNKNOWN, "private-old-value", False, "admin"))
    before = (await raw_rows(database))[UNKNOWN]
    models = clean_environment / "models.yaml"
    models.write_text(
        "models:\n  - id: sample-model\n    base_url: https://example.invalid/v1\n"
        "    api_key: ${UNCLAIMED_LEGACY_ENDPOINT}\n"
    )
    hidden = await snapshot(clean_environment, [UNKNOWN], **{UNKNOWN: "private-environment-value"})
    assert hidden["resolved"][UNKNOWN] is None
    assert UNKNOWN not in {entry["key"] for entry in hidden["body"]["entries"]}
    assert "private-old-value" not in json.dumps(hidden)
    assert "private-environment-value" not in json.dumps(hidden)

    # A normal edit cannot implicitly adopt an old row.
    code, _, stderr = await child(
        ["-m", "serving.config.manage", "set", UNKNOWN, "replacement"], clean_environment
    )
    assert code != 0
    assert "private-old-value" not in stderr

    code, stdout, stderr = await child(
        ["-m", "serving.config.manage", "adopt-custom", UNKNOWN], clean_environment
    )
    assert code == 0, stderr
    assert "private-old-value" not in stdout + stderr
    after = (await raw_rows(database))[UNKNOWN]
    assert after["owner"] == "custom"
    assert {key: after[key] for key in before if key != "secret"} == {
        key: value for key, value in before.items() if key != "secret"
    }
    adopted = await snapshot(clean_environment, [UNKNOWN])
    assert adopted["resolved"][UNKNOWN] == "private-old-value"
    entry = next(entry for entry in adopted["body"]["entries"] if entry["key"] == UNKNOWN)
    # The model's api_key reference still makes adopted custom data secret.
    assert entry["secret"] is True
    assert "private-old-value" not in json.dumps(adopted["body"])
    code, stdout, stderr = await child(
        ["-m", "serving.config.manage", "set", UNKNOWN, "updated-custom-value"],
        clean_environment,
    )
    assert code == 0, stderr
    assert "updated-custom-value" not in stdout + stderr
    assert (await raw_rows(database))[UNKNOWN]["owner"] == "custom"
    updated = await snapshot(clean_environment, [UNKNOWN])
    assert updated["resolved"][UNKNOWN] == "updated-custom-value"


async def test_legacy_claim_preserves_metadata_reset_and_secret_monotonicity(database):
    await legacy_rows(
        database,
        (REGION, "legacy-region", True, "env_import"),
        (SECRET, "legacy-credential", False, "admin"),
        (RESET, "", True, "reset"),
        ("SITE_NAME", "Old site", False, "admin"),
        (UNKNOWN, "not-claimed", False, "admin"),
    )
    before = await raw_rows(database)
    store = AppConfigStore(database)
    await store.ensure_schema()
    await store.ensure_schema()
    declaration = migration()
    await store.apply_migrations([declaration], core_keys=("SITE_NAME",), updated_by="migration")
    after = await raw_rows(database)
    for key, original in before.items():
        assert {field: after[key][field] for field in original if field != "secret"} == {
            field: value for field, value in original.items() if field != "secret"
        }
    assert all(after[key]["secret"] for key in (REGION, SECRET, RESET))
    assert all(after[key]["owner"] == OWNER for key in (REGION, SECRET, RESET))
    assert after["SITE_NAME"]["owner"] == "core"
    assert after[UNKNOWN]["owner"] is None
    assert RESET not in await store.fetch_all()
    assert (await store.fetch_all(include_reset=True))[RESET].source == "reset"
    async with database.acquire() as connection:
        record = dict(
            await connection.fetchrow(
                "SELECT * FROM app_config_ownership_migrations WHERE id = $1", declaration.id
            )
        )
    assert record["owner"] == OWNER
    assert record["checksum"] == declaration.checksum
    assert record["applied_by"] == "migration"

    await store.apply_migrations([declaration], core_keys=("SITE_NAME",), updated_by="second-boot")
    assert await raw_rows(database) == after
    async with database.acquire() as connection:
        assert (
            dict(
                await connection.fetchrow(
                    "SELECT * FROM app_config_ownership_migrations WHERE id = $1", declaration.id
                )
            )
            == record
        )


@pytest.mark.parametrize("change", ["keys", "secret_keys", "owner"])
async def test_changed_migration_declaration_rolls_back_the_entire_batch(database, change):
    await legacy_rows(
        database,
        (REGION, "region", False, "admin"),
        (SECRET, "credential", False, "admin"),
        (UNKNOWN, "untouched", False, "admin"),
    )
    store = AppConfigStore(database)
    await store.ensure_schema()
    original = migration(keys=(REGION,), secret_keys=())
    await store.apply_migrations([original])
    changed = registry.ConfigOwnershipMigration(
        id=original.id,
        owner="extension:other.delivery" if change == "owner" else OWNER,
        keys=(REGION, SECRET) if change == "keys" else (REGION,),
        secret_keys=(REGION,) if change == "secret_keys" else (),
    )
    unrelated = migration("unrelated-before-failure", keys=(UNKNOWN,), secret_keys=())
    before = await raw_rows(database)
    with pytest.raises(ConfigOwnershipError):
        await store.apply_migrations([unrelated, changed])
    assert await raw_rows(database) == before
    async with database.acquire() as connection:
        assert (
            await connection.fetchval("SELECT count(*) FROM app_config_ownership_migrations") == 1
        )


async def test_claim_cannot_take_another_owner_and_rolls_back_earlier_claims(database):
    await legacy_rows(database, (REGION, "region", False, "admin"))
    store = AppConfigStore(database)
    await store.ensure_schema()
    await store.write([ConfigRow(SECRET, "other-owner", owner="extension:other.delivery")])
    before = await raw_rows(database)
    with pytest.raises(ConfigOwnershipError):
        await store.apply_migrations(
            [
                migration("first-claim", keys=(REGION,), secret_keys=()),
                migration("conflicting-claim", keys=(SECRET,), secret_keys=(SECRET,)),
            ]
        )
    assert await raw_rows(database) == before
    async with database.acquire() as connection:
        assert (
            await connection.fetchval("SELECT count(*) FROM app_config_ownership_migrations") == 0
        )


async def guarded_operation(store, operation, owner):
    if operation == "write":
        await store.write([ConfigRow(REGION, "replaced", owner=owner)])
    elif operation in {"insert", "replace_reset"}:
        await store.insert_missing(
            [ConfigRow(REGION, "imported", owner=owner)], replace_reset=operation == "replace_reset"
        )
    elif operation == "reset":
        await store.reset(REGION, secret=True, updated_by="intruder", owner=owner)
    elif operation == "delete":
        await store.delete(REGION, owner=owner)
    else:
        await store.mark_secret([REGION], owner=owner)


@pytest.mark.parametrize(
    "operation", ["write", "insert", "replace_reset", "reset", "delete", "mark_secret"]
)
@pytest.mark.parametrize("owner", [None, "custom", "extension:other.delivery"])
async def test_direct_store_operations_guard_ownership_at_sql_boundary(database, operation, owner):
    store = AppConfigStore(database)
    await store.ensure_schema()
    await store.write([ConfigRow(REGION, "owned", owner=OWNER)])
    if operation == "replace_reset":
        await store.reset(REGION, secret=False, updated_by="owner", owner=OWNER)
    before = await raw_rows(database)
    with pytest.raises(ConfigOwnershipError):
        await guarded_operation(store, operation, owner)
    assert await raw_rows(database) == before


@pytest.mark.parametrize(
    "operation", ["write", "insert", "replace_reset", "reset", "delete", "mark_secret"]
)
async def test_direct_store_operations_do_not_implicitly_adopt_unknown_rows(database, operation):
    await legacy_rows(
        database, (REGION, "unclaimed", False, "reset" if operation == "replace_reset" else "admin")
    )
    store = AppConfigStore(database)
    await store.ensure_schema()
    before = await raw_rows(database)
    with pytest.raises(ConfigOwnershipError):
        await guarded_operation(store, operation, "custom")
    assert await raw_rows(database) == before


async def test_owned_write_reset_import_and_delete_preserve_owner_and_secrecy(database):
    store = AppConfigStore(database)
    await store.ensure_schema()
    await store.write([ConfigRow(REGION, "secret-value", secret=True, owner=OWNER)])
    await store.write([ConfigRow(REGION, "replacement", secret=False, owner=OWNER)])
    await store.reset(REGION, secret=False, updated_by="owner", owner=OWNER)
    reset = (await store.fetch_all(include_reset=True))[REGION]
    assert reset.owner == OWNER
    assert reset.secret is True
    assert reset.source == "reset"
    assert reset.value == ""
    assert await store.insert_missing([ConfigRow(REGION, "environment", owner=OWNER)]) == []
    assert (await store.fetch_all(include_reset=True))[REGION] == reset
    assert await store.insert_missing(
        [ConfigRow(REGION, "generated", owner=OWNER)], replace_reset=True
    ) == [REGION]
    replaced = (await store.fetch_all())[REGION]
    assert replaced.owner == OWNER
    assert replaced.secret is True
    assert await store.delete(REGION, owner=OWNER) is True


async def test_migration_precedes_environment_import_and_retains_reset(database, monkeypatch):
    register_sample()
    await legacy_rows(
        database,
        (REGION, "stored-region", False, "admin"),
        (RESET, "", True, "reset"),
    )
    monkeypatch.setenv(REGION, "environment-region")
    monkeypatch.setenv(RESET, "reset-environment")
    monkeypatch.setenv(SECRET, "new-environment-credential")
    store = AppConfigStore(database)
    await app_config.load(store)
    rows = await store.fetch_all(include_reset=True)
    assert rows[REGION].owner == OWNER
    assert rows[REGION].value == "stored-region"
    assert rows[REGION].source == "admin"
    assert rows[RESET].owner == OWNER
    assert rows[RESET].source == "reset"
    assert rows[RESET].value == ""
    assert rows[SECRET].owner == OWNER
    assert rows[SECRET].source == "env_import"
    assert rows[SECRET].secret is True
    assert app_config.config_value(REGION) == "stored-region"
    assert app_config.config_value(RESET) == "reset-environment"
    assert "new-environment-credential" not in str(app_config.describe())


@pytest.mark.parametrize("first_operation", ["refresh", "write"])
async def test_database_recovery_claims_legacy_rows_before_refresh_or_first_write(
    database, monkeypatch, first_operation
):
    register_sample()
    unknown_reset = "UNCLAIMED_LEGACY_RESET"
    await legacy_rows(
        database,
        ("SITE_NAME", "stored-site", False, "admin"),
        (REGION, "stored-region", False, "admin"),
        (SECRET, "stored-credential", False, "env_import"),
        (RESET, "", True, "reset"),
        (UNKNOWN, "private-unknown", False, "admin"),
        (unknown_reset, "", True, "reset"),
    )
    before = await raw_rows(database)
    monkeypatch.setenv(REGION, "environment-region")
    monkeypatch.setenv(RESET, "reset-environment")
    monkeypatch.setenv(UNKNOWN, "unknown-environment")
    monkeypatch.setenv(unknown_reset, "unknown-reset-environment")

    # A genuinely closed asyncpg connection reproduces the first startup's
    # unavailable database without substituting a fake configuration store.
    lost = await postgres.connect()
    await lost.close()
    with pytest.raises(asyncpg.InterfaceError):
        await app_config.load(AppConfigStore(lost))
    app_config.use_environment(database_enabled=True)
    assert not app_config.loaded_from_database()
    app_config.attach(AppConfigStore(database))

    if first_operation == "refresh":
        await app_config.refresh()
        assert app_config.config_value(REGION) == "stored-region"
    else:
        await app_config.update({REGION: "operator-region"}, updated_by="recovering-admin")
        assert app_config.config_value(REGION) == "operator-region"
    assert app_config.loaded_from_database()
    assert app_config.config_value("SITE_NAME") == "stored-site"
    assert app_config.config_value(SECRET) == "stored-credential"
    assert app_config.config_value(RESET) == "reset-environment"
    assert app_config.config_value(UNKNOWN) is None
    assert app_config.config_value(unknown_reset) is None
    described = app_config.describe()
    assert {UNKNOWN, unknown_reset}.isdisjoint(entry["key"] for entry in described["entries"])
    for value in (
        "stored-credential",
        "private-unknown",
        "unknown-environment",
        "unknown-reset-environment",
    ):
        assert value not in str(described)

    after = await raw_rows(database)
    assert after["SITE_NAME"]["owner"] == "core"
    assert all(after[key]["owner"] == OWNER for key in (REGION, SECRET, RESET))
    assert after[SECRET]["secret"] is True
    assert after[RESET]["source"] == "reset"
    assert after[RESET]["value"] == ""
    assert after[RESET]["secret"] is True
    assert after[UNKNOWN]["owner"] is None
    assert after[unknown_reset]["owner"] is None
    for key, original in before.items():
        if key == REGION and first_operation == "write":
            assert after[key]["source"] == "admin"
            assert after[key]["updated_by"] == "recovering-admin"
            continue
        assert {field: after[key][field] for field in original if field != "secret"} == {
            field: value for field, value in original.items() if field != "secret"
        }
    async with database.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM app_config_ownership_migrations WHERE id = 'sample-delivery-v1'"
            )
            == 1
        )


def sample_child_environment(directory):
    (directory / "sample_owned_extension.py").write_text(
        textwrap.dedent(f"""
        from serving.config import app_config_registry as registry

        def register():
            registry.register_config_entries([
                registry.ConfigEntry({REGION!r}, 'email', 'Sample region.'),
                registry.ConfigEntry({SECRET!r}, 'email', 'Sample credential.', secret=True),
                registry.ConfigEntry({RESET!r}, 'email', 'Sample reset.'),
            ], owner={OWNER!r})
            registry.register_config_ownership_migration(
                registry.ConfigOwnershipMigration(
                    id='sample-child-v1', owner={OWNER!r},
                    keys=({REGION!r}, {SECRET!r}, {RESET!r}), secret_keys=({SECRET!r},),
                )
            )
    """)
    )
    return {
        "BACKEND_EXTENSIONS": "sample_owned_extension",
        "PYTHONPATH": str(directory) + os.pathsep + subprocess_env()["PYTHONPATH"],
    }


async def test_fresh_processes_enforce_loaded_owner_and_reject_environment_yaml_bypass(
    database, clean_environment
):
    await legacy_rows(
        database,
        (REGION, "private-region", False, "env_import"),
        (SECRET, "private-credential", False, "admin"),
        (RESET, "", True, "reset"),
    )
    env = sample_child_environment(clean_environment)
    keys = [REGION, SECRET, RESET]
    loaded = await snapshot(clean_environment, keys, **env, **{RESET: "reset-env"})
    assert loaded["resolved"] == {
        REGION: "private-region",
        SECRET: "private-credential",
        RESET: "reset-env",
    }
    entries = {entry["key"]: entry for entry in loaded["body"]["entries"]}
    assert all(key in entries for key in keys)
    assert entries[SECRET]["secret"] is True
    assert "private-credential" not in json.dumps(loaded["body"])
    owned = await raw_rows(database)
    assert all(owned[key]["owner"] == OWNER for key in keys)

    (clean_environment / "models.yaml").write_text(
        f"models:\n  - id: sample-model\n    base_url: ${{{REGION}}}\n"
        f"    api_key: ${{{SECRET}}}\n    provider_model_id: ${{{RESET}}}\n"
    )
    unloaded = await snapshot(
        clean_environment, keys, **{key: f"environment-{key}" for key in keys}
    )
    assert unloaded["resolved"] == dict.fromkeys(keys)
    assert set(keys).isdisjoint(entry["key"] for entry in unloaded["body"]["entries"])
    assert "private-region" not in json.dumps(unloaded)
    assert "private-credential" not in json.dumps(unloaded)
    assert "environment-" not in json.dumps(unloaded)
    for command in (["set", REGION, "takeover"], ["reset", REGION], ["adopt-custom", REGION]):
        code, stdout, stderr = await child(
            ["-m", "serving.config.manage", *command], clean_environment
        )
        assert code != 0
        assert "private-credential" not in stdout + stderr
    assert await raw_rows(database) == owned
    reloaded = await snapshot(clean_environment, keys, **env, **{RESET: "new-reset-env"})
    assert reloaded["resolved"][REGION] == "private-region"
    assert reloaded["resolved"][RESET] == "new-reset-env"


async def wait_for_lock(observer, pid, *, advisory):
    async def waiting():
        while True:
            await observer.execute("SELECT pg_stat_clear_snapshot()")
            row = await observer.fetchrow(
                "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = $1", pid
            )
            if (
                row
                and row["wait_event_type"] == "Lock"
                and (not advisory or row["wait_event"] == "advisory")
            ):
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(waiting(), timeout=5)


async def test_two_startup_connections_serialize_migration_and_import_after_admin_write(database):
    await legacy_rows(database, (REGION, "legacy-region", False, "admin"))
    store = AppConfigStore(database)
    first = await postgres.connect()
    second = await postgres.connect()
    worker = None
    try:
        second_pid = await second.fetchval("SELECT pg_backend_pid()")

        async def boot_second():
            async with AppConfigStore(second).transaction() as transaction:
                await transaction.ensure_schema()
                await transaction.apply_migrations([migration(keys=(REGION,), secret_keys=())])
                return await transaction.insert_missing(
                    [ConfigRow(REGION, "stale-environment", source="env_import", owner=OWNER)]
                )

        async with AppConfigStore(first).transaction() as transaction:
            await transaction.ensure_schema()
            await transaction.apply_migrations([migration(keys=(REGION,), secret_keys=())])
            await transaction.write(
                [ConfigRow(REGION, "new-admin-value", updated_by="admin", owner=OWNER)]
            )
            worker = asyncio.create_task(boot_second())
            await wait_for_lock(first, second_pid, advisory=True)
            assert not worker.done()
        assert await asyncio.wait_for(worker, timeout=5) == []
        row = (await store.fetch_all())[REGION]
        assert row.value == "new-admin-value"
        assert row.source == "admin"
        assert row.updated_by == "admin"
        assert row.owner == OWNER
        async with database.acquire() as connection:
            assert (
                await connection.fetchval("SELECT count(*) FROM app_config_ownership_migrations")
                == 1
            )
    finally:
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await first.close()
        await second.close()


@pytest.mark.parametrize("operation", ["write", "replace_reset", "reset", "delete", "mark_secret"])
async def test_sql_guard_rechecks_owner_after_waiting_for_a_concurrent_row_change(
    database, operation
):
    store = AppConfigStore(database)
    await store.ensure_schema()
    await store.write([ConfigRow(REGION, "original", owner=OWNER)])
    if operation == "replace_reset":
        await store.reset(REGION, secret=False, updated_by="owner", owner=OWNER)
    blocker = await postgres.connect()
    writer = await postgres.connect()
    transaction = blocker.transaction()
    await transaction.start()
    task = None
    released = False
    try:
        writer_pid = await writer.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchrow("SELECT key FROM app_config WHERE key = $1 FOR UPDATE", REGION)
        task = asyncio.create_task(guarded_operation(AppConfigStore(writer), operation, OWNER))
        await wait_for_lock(blocker, writer_pid, advisory=False)
        assert not task.done()
        # Deliberate raw SQL models an owner change after the caller read the row.
        # The operation must recheck ownership in the eventual write, not only
        # in a preceding SELECT or in the process's configuration cache.
        await blocker.execute(
            "UPDATE app_config SET owner = 'extension:other.delivery' WHERE key = $1", REGION
        )
        await transaction.commit()
        released = True
        with pytest.raises(ConfigOwnershipError):
            await asyncio.wait_for(task, timeout=5)
        row = (await store.fetch_all(include_reset=True))[REGION]
        assert row.owner == "extension:other.delivery"
        assert row.value == ("" if operation == "replace_reset" else "original")
        assert row.secret is False
    finally:
        if not released:
            await transaction.rollback()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await blocker.close()
        await writer.close()
