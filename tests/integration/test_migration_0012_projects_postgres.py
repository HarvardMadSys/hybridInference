"""``0012_projects``: the project model and the backfill that fills it."""

from __future__ import annotations

import pytest

from serving.storage.project_backfill import (
    backfill_projects,
    default_project_id,
    personal_org_id,
)
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_previous_release_keeps_working_while_it_runs(scratch):
    await mig.upgrade(scratch, "0011_trim_oversized_preferences")
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


async def test_backfill_creates_the_personal_org_and_is_idempotent(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, user_name) "
            "VALUES ('u-bf', 'bf@x.test', 'h', 'Backfilled')"
        )
        await conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id) "
            "VALUES ('h-bf', 'hyi-bf', 'u-bf', 'u-bf')"
        )

        first = await backfill_projects(conn)
        second = await backfill_projects(conn)

        org = await conn.fetchrow(
            "SELECT id, name, kind FROM organizations WHERE id = $1", personal_org_id("u-bf")
        )
        member = await conn.fetchrow(
            "SELECT role FROM org_members WHERE org_id = $1 AND user_id = 'u-bf'",
            personal_org_id("u-bf"),
        )
        project = await conn.fetchrow(
            "SELECT id, name, is_default, status FROM projects WHERE id = $1",
            default_project_id("u-bf"),
        )
        key = await conn.fetchrow("SELECT org_id, project_id FROM api_keys WHERE key_hash = 'h-bf'")

    assert first == 1
    assert second == 0
    assert dict(org) == {"id": personal_org_id("u-bf"), "name": "Backfilled", "kind": "personal"}
    assert member["role"] == "owner"
    assert dict(project) == {
        "id": default_project_id("u-bf"),
        "name": "Default",
        "is_default": True,
        "status": "active",
    }
    assert key["org_id"] == personal_org_id("u-bf")
    assert key["project_id"] == default_project_id("u-bf")


async def test_backfill_repairs_membership_without_a_default_project(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ('u-partial', 'partial@x.test', 'h')"
        )
        await conn.execute(
            "INSERT INTO organizations (id, name, kind) VALUES ('org_p_u-partial', 'Partial', 'personal')"
        )
        await conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES ('org_p_u-partial', 'u-partial', 'owner')"
        )
        assert await backfill_projects(conn) == 1
        assert (
            await conn.fetchval("SELECT id FROM projects WHERE id = 'proj_d_u-partial'")
            == "proj_d_u-partial"
        )
        assert await backfill_projects(conn) == 0
