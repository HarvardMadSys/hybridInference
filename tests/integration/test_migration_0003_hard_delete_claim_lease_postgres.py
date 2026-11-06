"""``0003_hard_delete_claim_lease``: claim lease columns on ``users``."""
from __future__ import annotations
import pytest
from tests.fixtures import migrations as mig
pytestmark = [pytest.mark.integration, pytest.mark.dbtest]
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
    await mig.upgrade(scratch, "0002_api_key_limits")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, status, hard_delete_pending, hard_delete_claim_token) "
            "VALUES ('legacy-u', 'legacy@test.test', 'deleted', TRUE, 'legacy-token')"
        )
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        cols = {r["column_name"]: dict(r) for r in await conn.fetch(_COLUMNS)}
        row = await conn.fetchrow("SELECT hard_delete_claimed_at, hard_delete_claim_recovered FROM users WHERE id='legacy-u'")
    assert cols["hard_delete_claimed_at"]["data_type"] == "timestamp with time zone"
    assert cols["hard_delete_claimed_at"]["is_nullable"] == "YES"
    assert cols["hard_delete_claim_recovered"]["is_nullable"] == "NO"
    assert row["hard_delete_claimed_at"] is not None  # bounded backfill stamped it
    assert row["hard_delete_claim_recovered"] is False  # constant default

async def test_downgrade_keeps_columns(scratch):
    await mig.upgrade(scratch)
    import asyncio as _asyncio
    from alembic import command as _command
    from migrations import alembic_config as _alembic_config
    await _asyncio.to_thread(_command.downgrade, _alembic_config(mig.url(scratch)), "0002_api_key_limits")
    async with mig.connected(scratch) as conn:
        n = await conn.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_name='users' AND column_name='hard_delete_claimed_at'")
    assert n == 1
    # re-upgrade is idempotent
    await mig.upgrade(scratch)
