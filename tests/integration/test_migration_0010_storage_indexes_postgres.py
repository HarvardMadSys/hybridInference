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


_DROPPED = ("idx_api_logs_served_endpoint", "idx_api_logs_user")


async def test_the_api_logs_indexes_no_query_uses_go(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    assert set(await _indexes(scratch, *_DROPPED, "idx_api_logs_user_cost")) == {
        *_DROPPED,
        "idx_api_logs_user_cost",
    }

    await mig.upgrade(scratch, REVISION)

    assert set(await _indexes(scratch, *_DROPPED, "idx_api_logs_user_cost")) == {
        "idx_api_logs_user_cost"
    }


#: The shapes every lookup of one account's rows takes: its newest rows, a time
#: range of them, its first row, and several accounts' ranges at once.
_BY_ACCOUNT = (
    "SELECT request_id FROM api_logs WHERE user_id = 'u-1' ORDER BY timestamp DESC LIMIT 20",
    "SELECT count(*) FROM api_logs WHERE user_id = 'u-1' "
    "AND timestamp >= NOW() - INTERVAL '30 days'",
    "SELECT MIN(timestamp) FROM api_logs WHERE user_id = 'u-1'",
    "SELECT user_id, count(*) FROM api_logs WHERE user_id = ANY(ARRAY['u-1', 'u-2']) "
    "AND timestamp >= NOW() - INTERVAL '30 days' GROUP BY user_id",
)


@pytest.mark.parametrize("query", _BY_ACCOUNT)
async def test_lookups_by_account_are_still_index_scans_of_the_account(scratch, query):
    await mig.upgrade(scratch, REVISION)
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO api_logs (request_id, model_id, provider, user_id, timestamp) "
            "SELECT 'r-' || g, 'm', 'p', 'u-' || (g % 50), NOW() - g * INTERVAL '1 minute' "
            "FROM generate_series(1, 5000) AS g"
        )
        await conn.execute("ANALYZE api_logs")
        await conn.execute("SET enable_seqscan = off")
        await conn.execute("SET enable_bitmapscan = off")
        plan = "\n".join(r[0] for r in await conn.fetch("EXPLAIN " + query))
    assert "idx_api_logs_user_cost" in plan, plan


async def test_its_statements_can_run_again(scratch):
    """The schema-local test tables apply them after the baseline; running twice is harmless."""
    await mig.upgrade(scratch, REVISION)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")
    async with mig.connected(scratch) as conn:
        for statement in (*revision.STATEMENTS, *revision.INDEXES.values()):
            await conn.execute(statement)
    assert all(valid for valid, _ in (await _indexes(scratch, *revision.INDEXES)).values())
    assert await _indexes(scratch, *_DROPPED) == {}
