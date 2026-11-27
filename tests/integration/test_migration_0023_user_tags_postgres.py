"""``0023_user_tags``: user tags, their memberships and the built-in ``unattended`` tag."""

from __future__ import annotations

import importlib

import asyncpg
import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

REVISION = "0023_user_tags"


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_tables_arrive_with_only_the_built_in_tag(scratch):
    await mig.upgrade(scratch, "0022_ledger_currency")
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO users (id, email) VALUES ('u-old', 'old@example.edu')")

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        tags = await conn.fetch("SELECT * FROM user_tags")
        members = await conn.fetchval("SELECT COUNT(*) FROM user_tag_members")
        user = await conn.fetchrow("SELECT email, status FROM users WHERE id = 'u-old'")

    assert len(tags) == 1
    unattended = dict(tags[0])
    assert unattended["name"] == "unattended"
    assert unattended["system"] is True
    assert unattended["auto_enabled"] is True
    assert (unattended["auto_window_hours"], unattended["auto_min_active_hours"]) == (12, 12)
    assert unattended["quota_daily_cost"] is None
    assert unattended["quota_monthly_cost"] is None
    assert unattended["max_concurrent_requests"] is None
    assert unattended["allowed_models"] is None
    assert members == 0
    assert dict(user) == {"email": "old@example.edu", "status": "active"}


async def test_a_membership_leaves_with_its_account_and_its_tag(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await conn.execute("INSERT INTO users (id, email) VALUES ('u1', 'a@example.edu')")
        await conn.execute("INSERT INTO users (id, email) VALUES ('u2', 'b@example.edu')")
        await conn.execute("INSERT INTO user_tags (name) VALUES ('lab')")
        await conn.execute(
            "INSERT INTO user_tag_members (user_id, tag, source) VALUES "
            "('u1', 'unattended', 'auto'), ('u2', 'unattended', 'manual'), ('u2', 'lab', 'manual')"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO user_tag_members (user_id, tag, source) VALUES ('u1', 'lab', 'other')"
            )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO user_tag_members (user_id, tag, source) "
                "VALUES ('u1', 'unattended', 'manual')"
            )

        await conn.execute("DELETE FROM users WHERE id = 'u1'")
        await conn.execute("DELETE FROM user_tags WHERE name = 'lab'")
        remaining = await conn.fetch("SELECT user_id, tag FROM user_tag_members")

    assert [tuple(row) for row in remaining] == [("u2", "unattended")]


async def test_its_statements_can_run_again(scratch):
    """Going back up after a downgrade, which keeps the tables, still works."""
    await mig.upgrade(scratch)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "UPDATE user_tags SET max_concurrent_requests = 2 WHERE name = 'unattended'"
        )
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
        cap = await conn.fetchval(
            "SELECT max_concurrent_requests FROM user_tags WHERE name = 'unattended'"
        )
    # The seed did not overwrite what an administrator set.
    assert cap == 2
