"""``0003_batch_no_expiry``: ``batch_jobs.expires_at`` becomes nullable."""

from __future__ import annotations

import pytest

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_NULLABLE = """
    SELECT is_nullable FROM information_schema.columns
    WHERE table_name = 'batch_jobs' AND column_name = 'expires_at'
"""


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_an_existing_batch_keeps_its_expiry_and_a_new_one_may_have_none(scratch):
    await mig.upgrade(scratch, "0002_api_key_limits")
    async with mig.connected(scratch) as conn:
        assert await conn.fetchval(_NULLABLE) == "NO"
        await conn.execute(
            "INSERT INTO batch_jobs (id, user_id, endpoint, expires_at) "
            "VALUES ('batch_old', 'u-old', '/v1/chat/completions', "
            "'2026-11-07T00:00:00Z')"
        )

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        assert await conn.fetchval(_NULLABLE) == "YES"
        assert await conn.fetchval(
            "SELECT expires_at IS NOT NULL FROM batch_jobs WHERE id = 'batch_old'"
        )
        await conn.execute(
            "INSERT INTO batch_jobs (id, user_id, endpoint, expires_at) "
            "VALUES ('batch_new', 'u-new', '/v1/chat/completions', NULL)"
        )
        assert await conn.fetchval("SELECT count(*) FROM batch_jobs") == 2
