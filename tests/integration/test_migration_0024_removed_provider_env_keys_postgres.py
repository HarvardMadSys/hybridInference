"""``0024_removed_provider_env_keys``: a removed flag on env-key tombstones."""

from __future__ import annotations

import importlib

import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

REVISION = "0024_removed_provider_env_keys"


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_an_existing_tombstone_stays_re_enableable(scratch):
    """A row written before the column existed is disabled, not removed."""
    await mig.upgrade(scratch, "0023_user_tags")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO disabled_provider_env_keys (provider, key_hash, key_prefix) "
            "VALUES ('zai', 'h-old', 'sk-old')"
        )

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        row = await conn.fetchrow(
            "SELECT removed, is_nullable, column_default "
            "FROM disabled_provider_env_keys "
            "JOIN information_schema.columns "
            "ON table_name = 'disabled_provider_env_keys' AND column_name = 'removed' "
            "WHERE key_hash = 'h-old'"
        )
    assert row["removed"] is False
    assert row["is_nullable"] == "NO"
    assert "false" in row["column_default"].lower()


async def test_its_statement_can_run_again(scratch):
    """Going back up after a downgrade, which keeps the column, still works.

    Re-running the expand must not clear a row an administrator already removed.
    """
    await mig.upgrade(scratch)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO disabled_provider_env_keys "
            "(provider, key_hash, key_prefix, removed) "
            "VALUES ('zai', 'h-kept', 'sk-kept', TRUE)"
        )
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
        removed = await conn.fetchval(
            "SELECT removed FROM disabled_provider_env_keys WHERE key_hash = 'h-kept'"
        )
    assert removed is True
