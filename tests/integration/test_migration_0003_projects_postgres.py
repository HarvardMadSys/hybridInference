"""``0003_projects``: the project model and the backfill that fills it."""

from __future__ import annotations

import pytest

from serving.storage.project_backfill import (
    backfill_projects,
    default_project_id,
    personal_org_id,
    seed_spend_counters,
)
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_previous_release_keeps_working_while_it_runs(scratch):
    await mig.upgrade(scratch, "0002_api_key_limits")
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


async def test_spend_counters_are_seeded_once(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ('u-sp', 'sp@x.test', 'h')"
        )
        await conn.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests) "
            "VALUES ('u-sp', '2026-11-05', 4.25, 7)"
        )

        first = await seed_spend_counters(conn)
        second = await seed_spend_counters(conn)

        row = await conn.fetchrow(
            "SELECT cost_usd, requests FROM spend_counters "
            "WHERE scope_id = $1 AND period = 'd:2026-11-05'",
            personal_org_id("u-sp"),
        )

    assert first == 1
    assert second == 0
    assert float(row["cost_usd"]) == pytest.approx(4.25)
    assert row["requests"] == 7
