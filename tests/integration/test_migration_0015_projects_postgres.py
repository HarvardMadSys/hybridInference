"""``0015_projects``: the project model.

The backfill that fills it is tested with ``0018_projects_owned_by_accounts``,
which changed who owns a project.
"""

from __future__ import annotations

import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


@pytest.mark.parametrize(
    "previous_revision", ["0011_trim_oversized_preferences", "0014_legacy_catalog_mirror"]
)
async def test_the_previous_release_keeps_working_while_it_runs(scratch, previous_revision):
    await mig.upgrade(scratch, previous_revision)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ('u-old', 'old@x.test', 'h')"
        )
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id) "
            "VALUES ('h-old', 'hyi-old', 'u-old', 'u-old')"
        )

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        key = await conn.fetchrow(
            "SELECT org_id, project_id FROM api_keys WHERE key_hash = 'h-old'"
        )
        tables = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        }
    assert key["org_id"] is None
    assert key["project_id"] is None
    assert {"organizations", "org_members", "projects", "spend_counters"} <= tables
