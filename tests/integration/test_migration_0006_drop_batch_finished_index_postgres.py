"""``0006_drop_batch_finished_index``: drop the batch index no query can use."""

from __future__ import annotations

import importlib

import pytest

from migrations.compat import read_state
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

PREVIOUS = "0005_drop_request_id_index"
REVISION = "0006_drop_batch_finished_index"

_INDEX = "SELECT to_regclass('idx_batch_jobs_finished') IS NOT NULL"


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_index_goes(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        assert await conn.fetchval(_INDEX)

    await mig.upgrade(scratch)

    async with mig.connected(scratch) as conn:
        assert not await conn.fetchval(_INDEX)
        assert (await read_state(conn)).revision == REVISION


async def test_the_purge_search_never_used_it(scratch):
    """The purge reckons a batch's end from its sealing first; the index did not."""
    await mig.upgrade(scratch, PREVIOUS)
    from serving.storage.batch_store import _PURGE_CANDIDATES_SQL

    async with mig.connected(scratch) as conn:
        await conn.execute("SET enable_seqscan = off")
        plan = "\n".join(
            r[0]
            for r in await conn.fetch(
                "EXPLAIN " + _PURGE_CANDIDATES_SQL,
                7,
                100,
                [],
            )
        )
    assert "idx_batch_jobs_finished" not in plan


async def test_its_statements_can_run_again(scratch):
    await mig.upgrade(scratch)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
        assert not await conn.fetchval(_INDEX)
