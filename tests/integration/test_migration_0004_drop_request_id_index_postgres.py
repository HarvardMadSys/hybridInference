"""``0004_drop_request_id_index``: drop the copy of the unique index on ``api_logs.request_id``.

The drop is concurrent: log writes go on while it waits, a long transaction on
the table stops it after the bounded wait, and the next run finishes it. A
database whose ``request_id`` has no unique index keeps the one it has.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from migrations import LOCK_NOT_AVAILABLE, sqlstate
from migrations.compat import read_state
from tests.fixtures import migrations as mig, postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

PREVIOUS = "0003_user_usage_sums"
REVISION = "0004_drop_request_id_index"

_INDEXES = """
    SELECT c.relname, i.indisunique, i.indisvalid
    FROM pg_index AS i JOIN pg_class AS c ON c.oid = i.indexrelid
    WHERE i.indrelid = 'api_logs'::regclass
      AND c.relname IN ('idx_api_logs_request_id', 'api_logs_request_id_key')
    ORDER BY c.relname
"""


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _indexes(database: str) -> dict[str, tuple[bool, bool]]:
    async with mig.connected(database) as conn:
        return {
            r["relname"]: (r["indisunique"], r["indisvalid"]) for r in await conn.fetch(_INDEXES)
        }


async def _revision(database: str) -> str | None:
    async with mig.connected(database) as conn:
        return (await read_state(conn)).revision


async def test_the_copy_goes_and_the_unique_index_still_serves_writes_and_lookups(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    assert await _indexes(scratch) == {
        "api_logs_request_id_key": (True, True),
        "idx_api_logs_request_id": (False, True),
    }

    await mig.upgrade(scratch)

    assert await _indexes(scratch) == {"api_logs_request_id_key": (True, True)}
    assert await _revision(scratch) == REVISION
    async with mig.connected(scratch) as conn:
        # The previous release's write: ON CONFLICT arbitrates on the unique index.
        for _ in range(2):
            await conn.execute(
                "INSERT INTO api_logs (request_id, model_id, provider) VALUES ('r-1', 'm', 'p') "
                "ON CONFLICT (request_id) DO NOTHING"
            )
        assert await conn.fetchval("SELECT count(*) FROM api_logs") == 1
        await conn.execute("SET enable_seqscan = off")
        plan = "\n".join(
            r[0]
            for r in await conn.fetch("EXPLAIN SELECT * FROM api_logs WHERE request_id = 'r-1'")
        )
    assert "api_logs_request_id_key" in plan


async def test_a_database_whose_request_id_is_not_unique_keeps_its_only_index(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    async with mig.connected(scratch) as conn:
        await conn.execute("ALTER TABLE api_logs DROP CONSTRAINT api_logs_request_id_key")

    await mig.upgrade(scratch)

    assert await _indexes(scratch) == {"idx_api_logs_request_id": (False, True)}
    assert await _revision(scratch) == REVISION


async def test_writes_pass_while_it_waits_and_a_long_reader_stops_it_until_the_rerun(scratch):
    """A transaction holding ``api_logs`` -- a report, ``pg_dump`` -- outlasts the 3 s wait.

    A plain DROP INDEX would queue an ACCESS EXCLUSIVE lock and every log write
    would wait behind it; the concurrent drop lets them through. When the wait
    runs out the revision is not recorded, and the index it had begun to drop is
    left invalid: still maintained by writes, no longer read. The next run
    finishes the drop.
    """
    await mig.upgrade(scratch, PREVIOUS)
    reader = await postgres.connect(database=scratch)
    reading = reader.transaction()
    await reading.start()
    writer = await postgres.connect(database=scratch)
    try:
        await reader.execute("SELECT count(*) FROM api_logs")
        run = asyncio.ensure_future(mig.upgrade(scratch))
        for _ in range(200):
            if await writer.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query LIKE 'DROP INDEX CONCURRENTLY%' AND wait_event_type = 'Lock'"
            ):
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("the drop never waited")
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(
            writer.execute(
                "INSERT INTO api_logs (request_id, model_id, provider) VALUES ('r-2', 'm', 'p')"
            ),
            2,
        )
        write_waited = loop.time() - started
        with pytest.raises(Exception) as caught:
            await asyncio.wait_for(run, 30)
    finally:
        await reading.rollback()
        await reader.close()
        await writer.close()

    assert write_waited < 1
    assert sqlstate(caught.value) == LOCK_NOT_AVAILABLE
    assert await _revision(scratch) == PREVIOUS
    assert await _indexes(scratch) == {
        "api_logs_request_id_key": (True, True),
        "idx_api_logs_request_id": (False, False),
    }

    await mig.upgrade(scratch)

    assert await _indexes(scratch) == {"api_logs_request_id_key": (True, True)}
    assert await _revision(scratch) == REVISION


async def test_its_statements_can_run_again(scratch):
    """The schema-local test tables apply them after the baseline; running twice is harmless."""
    await mig.upgrade(scratch)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        for statement in revision.STATEMENTS:
            await conn.execute(statement)
    assert await _indexes(scratch) == {"api_logs_request_id_key": (True, True)}
