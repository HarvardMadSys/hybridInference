"""The database-backed configuration: ``app_config``, its revision counter, instances.

Frozen copies of ``serving.config.app_config_store.CREATE_TABLE_SQL``, the
revision table, trigger and base record of ``serving.config.revisions``, and
``serving.config.instances.CREATE_INSTANCES_SQL``.
"""

from __future__ import annotations

from typing import Any

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS app_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    secret      BOOLEAN NOT NULL DEFAULT FALSE,
    source      TEXT NOT NULL DEFAULT 'admin',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  TEXT
)
"""


#: The source name of the ``app_config`` table.
APP_CONFIG_SOURCE = "app_config"
#: A fresh epoch, generated in the database: 16 hex characters.
_NEW_EPOCH_SQL = "substr(md5(random()::text || clock_timestamp()::text), 1, 16)"
#: Serializes schema creation and upgrades across processes booting at once.
SCHEMA_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hybridinference:config-schema'))"


CREATE_REVISIONS_SQL = """
CREATE TABLE IF NOT EXISTS config_source_revisions (
    source      TEXT PRIMARY KEY,
    epoch       TEXT NOT NULL,
    revision    BIGINT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    contracts   JSONB NOT NULL DEFAULT '{}'::jsonb
)
"""


# The base record: revision 0, no active group. Never overwrites an existing
# row, so a restart cannot reset the history or the contracts.
SEED_REVISION_SQL = f"""
INSERT INTO config_source_revisions (source, epoch, revision, contracts)
VALUES ($1, {_NEW_EPOCH_SQL}, 0, '{{}}'::jsonb)
ON CONFLICT (source) DO NOTHING
"""


# Explicit insert-or-update, so a missing counter row is created rather than
# a write going uncounted. The new row's epoch is fresh: losing the row is a
# break in the history, not a continuation of it.
BUMP_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION config_source_bump_app_config() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO config_source_revisions AS r (source, epoch, revision, updated_at)
    VALUES ('{APP_CONFIG_SOURCE}', {_NEW_EPOCH_SQL}, 1, NOW())
    ON CONFLICT (source) DO UPDATE SET revision = r.revision + 1, updated_at = NOW();
    RETURN NULL;
END
$$
"""


TRIGGER_NAME = "app_config_source_revision"
CREATE_TRIGGER_SQL = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = '{TRIGGER_NAME}' AND tgrelid = 'app_config'::regclass
    ) THEN
        CREATE TRIGGER {TRIGGER_NAME}
            BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON app_config
            FOR EACH STATEMENT EXECUTE FUNCTION config_source_bump_app_config();
    END IF;
END
$$
"""


CREATE_INSTANCES_SQL = """
CREATE TABLE IF NOT EXISTS config_instances (
    instance_id   TEXT PRIMARY KEY,
    build         TEXT NOT NULL,
    capabilities  JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at    TIMESTAMPTZ NOT NULL,
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lifecycle     TEXT NOT NULL,
    report_seq    BIGINT NOT NULL DEFAULT 0,
    groups        JSONB NOT NULL DEFAULT '{}'::jsonb
)
"""


async def ensure_schema(conn: Any, *, create_source_table: str | None = None) -> None:
    """Create the revision and instance tables and the trigger; idempotent.

    Runs in one transaction under :data:`SCHEMA_LOCK_SQL`, so workers booting
    together neither race ``CREATE TABLE IF NOT EXISTS`` (which can fail on a
    concurrent creation) nor ``CREATE OR REPLACE FUNCTION``. The base record
    has no active group.

    Args:
        conn: The connection.
        create_source_table: The ``app_config`` ``CREATE TABLE`` statement,
            run first under the same lock; ``None`` when the table exists.
    """
    async with conn.transaction():
        await conn.execute(SCHEMA_LOCK_SQL)
        if create_source_table is not None:
            await conn.execute(create_source_table)
        await conn.execute(CREATE_REVISIONS_SQL)
        await conn.execute(CREATE_INSTANCES_SQL)
        await conn.execute(SEED_REVISION_SQL, APP_CONFIG_SOURCE)
        await conn.execute(BUMP_FUNCTION_SQL)
        await conn.execute(CREATE_TRIGGER_SQL)
