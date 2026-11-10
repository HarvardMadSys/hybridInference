"""At runtime the service issues no DDL, and it will not start on an unmigrated database.

The tripwire is a database that refuses DDL: migrated first, then given an event
trigger that raises on every DDL statement. The paths that used to build or
alter tables on the way (startup's stores, loading the configuration, the
public stats snapshot) run against it. The shared test database keeps no such
trigger, because many tests build old schemas on purpose.
"""

from __future__ import annotations

import pytest

from migrations.compat import SchemaNotReady, require_schema
from serving.analytics import public_stats
from serving.config import app_config
from serving.config.app_config_registry import static_entries
from serving.config.app_config_store import AppConfigStore
from serving.config.settings import get_settings
from serving.servers import bootstrap
from serving.storage.database import DatabaseLogger
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import migrations as mig, postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_FORBID_DDL = """
CREATE FUNCTION forbid_runtime_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'DDL at runtime: %', tg_tag;
END $$;
CREATE EVENT TRIGGER forbid_runtime_ddl ON ddl_command_start
    EXECUTE FUNCTION forbid_runtime_ddl();
"""


@pytest.fixture
async def ddl_forbidden():
    """A migrated database on which every further DDL statement raises."""
    async with mig.scratch_database() as name:
        await mig.upgrade(name)
        async with mig.connected(name) as conn:
            await conn.execute(_FORBID_DDL)
        yield name


@pytest.fixture
def config_env(monkeypatch, tmp_path):
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    get_settings.cache_clear()
    app_config.reset_state()
    yield
    app_config.reset_state()
    get_settings.cache_clear()


async def test_the_tripwire_trips(ddl_forbidden):
    async with mig.connected(ddl_forbidden) as conn:
        with pytest.raises(Exception, match="DDL at runtime"):
            await conn.execute("CREATE TABLE IF NOT EXISTS anything (id INT)")


async def test_startup_and_the_paths_that_used_to_build_tables_issue_no_ddl(
    ddl_forbidden, config_env
):
    logger = DatabaseLogger(
        postgres.target().connect_kwargs(ddl_forbidden),
        store_full_prompts=False,
        fence_secret=postgres.FENCE_SECRET,
    )
    await logger.initialize()
    try:
        await require_schema(logger.pool)
        await PostgresOperationalStore(logger.pool).initialize()
        await PostgresLogStore(logger.pool, fence_secret=postgres.FENCE_SECRET).initialize()
        async with logger.pool.acquire() as conn:
            await app_config.load(AppConfigStore(conn))
        app_config.attach(AppConfigStore(logger.pool))
        await app_config.refresh()
        await app_config.update({"SITE_NAME": "Tripwire"}, updated_by="test")
        async with logger.pool.acquire() as conn:
            snapshot = await public_stats.compute_snapshot(conn, models_config={})
            await public_stats.store_snapshot(conn, snapshot)
    finally:
        await logger.cleanup()


async def test_startup_refuses_a_database_the_migrations_have_not_reached(monkeypatch, config_env):
    async with mig.scratch_database() as name:
        for key, value in postgres.target().app_env().items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("DB_NAME", name)
        get_settings.cache_clear()

        with pytest.raises(SchemaNotReady, match="python -m migrations upgrade"):
            await bootstrap._load_app_config()
