"""``0010_storage_indexes``: the indexes it builds concurrently, and their interrupted builds.

A build waits for the transactions older than it, a long one stops it after the
bounded wait, and it leaves an invalid index behind, which the next run drops
and builds again.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from migrations import LOCK_NOT_AVAILABLE, sqlstate
from migrations.compat import read_state
from tests.fixtures import migrations as mig, postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

PREVIOUS = "0009_route_catalog"
REVISION = "0010_storage_indexes"

_INDEXES = """
    SELECT c.relname, i.indisvalid, pg_get_indexdef(i.indexrelid) AS definition
    FROM pg_index AS i JOIN pg_class AS c ON c.oid = i.indexrelid
    WHERE c.relname = ANY($1::text[])
"""


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _indexes(database: str, *names: str) -> dict[str, tuple[bool, str]]:
    async with mig.connected(database) as conn:
        return {
            r["relname"]: (r["indisvalid"], r["definition"])
            for r in await conn.fetch(_INDEXES, list(names))
        }


async def _revision(database: str) -> str | None:
    async with mig.connected(database) as conn:
        return (await read_state(conn)).revision


async def test_session_liveness_gets_its_index(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    assert await _indexes(scratch, "idx_auth_sessions_user_sid") == {}

    await mig.upgrade(scratch, REVISION)

    ((valid, definition),) = (await _indexes(scratch, "idx_auth_sessions_user_sid")).values()
    assert valid
    assert definition.endswith("ON public.auth_sessions USING btree (user_id, sid)")
    assert await _revision(scratch) == REVISION


async def test_a_long_transaction_stops_the_build_and_the_rerun_finishes_it(scratch):
    """A concurrent build waits for older transactions; past the bounded wait it fails.

    The revision is then not recorded and the index it began is left invalid --
    maintained, never read. Sign-ins go on while it waits. The next run drops
    the invalid index and builds it again.
    """
    await mig.upgrade(scratch, PREVIOUS)
    reader = await postgres.connect(database=scratch)
    # A snapshot older than the build, held to the end of the transaction, as
    # pg_dump holds one.
    reading = reader.transaction(isolation="repeatable_read")
    await reading.start()
    writer = await postgres.connect(database=scratch)
    try:
        await reader.execute("SELECT count(*) FROM auth_sessions")
        run = asyncio.ensure_future(mig.upgrade(scratch, REVISION))
        for _ in range(200):
            if await writer.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query LIKE 'CREATE INDEX CONCURRENTLY%' AND wait_event_type = 'Lock'"
            ):
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("the build never waited")
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(
            writer.execute(
                "INSERT INTO auth_sessions (id, user_id, refresh_token_hash, sid, expires_at) "
                "VALUES ('as-1', 'u-1', 'h-1', 's-1', NOW() + INTERVAL '1 day')"
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
    ((valid, _),) = (await _indexes(scratch, "idx_auth_sessions_user_sid")).values()
    assert not valid

    await mig.upgrade(scratch, REVISION)

    ((valid, _),) = (await _indexes(scratch, "idx_auth_sessions_user_sid")).values()
    assert valid
    assert await _revision(scratch) == REVISION


async def test_its_statements_can_run_again(scratch):
    await mig.upgrade(scratch, REVISION)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        for statement in revision.INDEXES.values():
            await conn.execute(statement)
    assert all(valid for valid, _ in (await _indexes(scratch, *revision.INDEXES)).values())
