"""``0016_hard_delete_claim_lease``: claim lease columns on ``users``."""

from __future__ import annotations

import asyncio

import pytest
from alembic import command

from migrations import alembic_config
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]
BASE_REVISION = "0015_projects"
_COLUMNS = """
    SELECT column_name, data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_name = 'users'
      AND column_name IN ('hard_delete_claimed_at', 'hard_delete_claim_recovered')
    ORDER BY column_name
"""


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_existing_pending_claim_gains_lease_columns(scratch):
    await mig.upgrade(scratch, BASE_REVISION)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, status, hard_delete_pending, hard_delete_claim_token) "
            "VALUES ('legacy-u', 'legacy@test.test', 'deleted', TRUE, 'legacy-token')"
        )
    await mig.upgrade(scratch, "0016_hard_delete_claim_lease")
    async with mig.connected(scratch) as conn:
        cols = {r["column_name"]: dict(r) for r in await conn.fetch(_COLUMNS)}
        row = await conn.fetchrow(
            "SELECT hard_delete_claimed_at, hard_delete_claim_recovered FROM users WHERE id='legacy-u'"
        )
        version = await conn.fetchval("SELECT version_num FROM hybridinference_alembic_version")
    assert cols["hard_delete_claimed_at"]["data_type"] == "timestamp with time zone"
    assert cols["hard_delete_claimed_at"]["is_nullable"] == "YES"
    assert cols["hard_delete_claim_recovered"]["is_nullable"] == "NO"
    assert row["hard_delete_claimed_at"] is not None  # bounded backfill stamped it
    assert row["hard_delete_claim_recovered"] is False  # constant default
    assert version == "0016_hard_delete_claim_lease"


async def test_a_claim_that_is_not_pending_is_left_null(scratch):
    """The backfill touches pending claims only; live accounts keep a NULL lease."""
    await mig.upgrade(scratch, BASE_REVISION)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, status) VALUES ('live-u', 'live@test.test', 'active')"
        )
    await mig.upgrade(scratch, "0016_hard_delete_claim_lease")
    async with mig.connected(scratch) as conn:
        row = await conn.fetchrow(
            "SELECT hard_delete_claimed_at, hard_delete_claim_recovered FROM users WHERE id='live-u'"
        )
    assert row["hard_delete_claimed_at"] is None
    assert row["hard_delete_claim_recovered"] is False


async def test_downgrade_keeps_columns(scratch):
    await mig.upgrade(scratch, "0016_hard_delete_claim_lease")
    await asyncio.to_thread(command.downgrade, alembic_config(mig.url(scratch)), BASE_REVISION)
    async with mig.connected(scratch) as conn:
        cols = {r["column_name"] for r in await conn.fetch(_COLUMNS)}
        version = await conn.fetchval("SELECT version_num FROM hybridinference_alembic_version")
    assert cols == {"hard_delete_claimed_at", "hard_delete_claim_recovered"}
    assert version == BASE_REVISION
    # re-upgrade is idempotent
    await mig.upgrade(scratch, "0016_hard_delete_claim_lease")
    async with mig.connected(scratch) as conn:
        assert await conn.fetchval("SELECT version_num FROM hybridinference_alembic_version") == (
            "0016_hard_delete_claim_lease"
        )
