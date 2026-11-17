"""A connection the server ended while it was out gives its place in the pool back.

The request pool is small (four connections), and the shared spend admission
has the server end a session that idles in its transaction. A connection so
ended and used once more, at the moment asyncpg has read the server's goodbye
and not yet seen the socket close, was aborted by the driver without being
cleaned up: its place in the pool was never free again. Four of those and no
request got a connection until the process restarted.
"""

from __future__ import annotations

import asyncio

import pytest

from serving.storage.pools import request_connection
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _close(pool) -> None:
    try:
        await asyncio.wait_for(pool.close(), 5)
    except TimeoutError:
        pool.terminate()


async def _used_after_the_server_ended_it(pool, watch, *, turns: int) -> str:
    """Use a request connection once more after the server ended its session; what that got.

    The session ends by its idle-in-transaction timeout while the client is
    not reading its socket. Reading resumes *turns* turns of the event loop
    before the next statement: with none the statement goes out before the
    goodbye is read, with two or three the goodbye has been read and the
    closed socket not yet seen, later the connection is known to be closed.
    """
    try:
        async with request_connection(pool, timeout=5) as conn:
            await conn.execute("BEGIN; SET LOCAL idle_in_transaction_session_timeout = '40ms'")
            pid = await conn.fetchval("SELECT pg_backend_pid()")
            transport = conn._transport
            transport.pause_reading()
            async with asyncio.timeout(10):
                while await watch.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid = $1", pid
                ):
                    await asyncio.sleep(0.01)
            transport.resume_reading()
            for _ in range(turns):
                await asyncio.sleep(0)
            await conn.fetchval("SELECT 2")
    except Exception as exc:
        return type(exc).__name__
    return "no error"


async def test_a_connection_the_server_ended_gives_its_place_back_whenever_it_is_next_used():
    pool = await postgres.create_pool(min_size=1, max_size=2)
    watch = await postgres.create_pool(min_size=1, max_size=1)
    try:
        seen = [
            await _used_after_the_server_ended_it(pool, watch, turns=turns) for turns in range(5)
        ]
        assert "no error" not in seen
        # The moment this is about was among them. If it no longer is (another
        # driver or event loop), this test has stopped covering free_if_closed.
        assert "InternalClientError" in seen, seen
        # Every place is there to be had, at once.
        async with asyncio.timeout(5):
            async with pool.acquire() as one, pool.acquire() as other:
                assert await one.fetchval("SELECT 1") == await other.fetchval("SELECT 1") == 1
    finally:
        await _close(pool)
        await _close(watch)
