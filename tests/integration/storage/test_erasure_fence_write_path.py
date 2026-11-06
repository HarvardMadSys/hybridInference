"""The statements an ``api_logs`` write sends under the erasure fence (issue #1421).

A write that names an account takes the shared locks in one statement and reads
the fence in the next -- the same statement as its INSERT -- then commits: four
round trips, where the lock per key and a separate fence SELECT took five or
six. A write that names no account is one INSERT. The fence must be read in a
statement started after the locks were granted; the tests here show what reading
it in the locking statement itself would miss.

``test_erasure_fence_races.py`` holds the protocol's other orderings.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
import pytest_asyncio

from serving.storage.database import DatabaseLogger
from serving.storage.erasure_fence import (
    establish_erasure_fence,
    fence_account_advisory_key,
    fence_account_digest,
    lock_fence_keys_shared,
)
from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest]

_SECRET = postgres.FENCE_SECRET
_DELETED = "u-fwp-deleted"
_OTHER = "u-fwp-other"


@pytest_asyncio.fixture
async def pool():
    """A pool on the test database, with no tombstones and none of this file's rows."""
    pool = await postgres.create_pool(min_size=2, max_size=6)
    store = PostgresLogStore(pool, fence_secret=_SECRET)
    await store.initialize()

    async def _wipe() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE request_id LIKE 'req-fwp-%'")
            await conn.execute("DELETE FROM erasure_fence")

    await _wipe()
    try:
        yield pool
    finally:
        await _wipe()
        await pool.close()


class _OneConnection:
    """A pool of one connection that sends nothing on release.

    So every statement logged on the connection was sent by the write itself.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        @contextlib.asynccontextmanager
        async def _acquired() -> Any:
            yield self._conn

        return _acquired()


#: What each statement a write sends is, by how it starts.
_KINDS = (
    ("BEGIN", "BEGIN"),
    ("COMMIT", "COMMIT"),
    ("SELECT pg_advisory_xact_lock_shared", "locks"),
    ("SELECT 1 FROM erasure_fence", "fence check"),
    ("WITH fence AS", "fence check and insert"),
    ("INSERT INTO api_logs", "insert"),
)


def _kind(query: str) -> str:
    text = " ".join(query.split())
    return next((kind for start, kind in _KINDS if text.startswith(start)), text)


async def _statements_of(conn: Any, write: Any) -> list[str]:
    """What each statement *conn* sends while *write* runs is, in order."""
    sent: list[str] = []

    def _record(query: Any) -> None:
        sent.append(_kind(query.query))

    conn.add_query_logger(_record)
    try:
        await write
        # Query loggers are called soon after, not during, each statement.
        await asyncio.sleep(0)
    finally:
        conn.remove_query_logger(_record)
    return sent


def _log(store: Any, request_id: str, metadata: dict[str, Any]) -> Any:
    return store.log_request(
        request_id=request_id,
        model_id="m",
        provider="p",
        prompt="hello",
        response={"text": "hi"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata=metadata,
    )


async def _logged(pool: Any, request_id: str) -> bool:
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval("SELECT count(*) FROM api_logs WHERE request_id = $1", request_id)
        )


@pytest.mark.parametrize("writer", ["store", "logger"])
@pytest.mark.parametrize(
    "metadata",
    [
        {"user_id": _OTHER},
        {"credential_owner_id": _OTHER},
        {"user_id": _OTHER, "credential_owner_id": "u-fwp-owner"},
    ],
    ids=["user", "credential-owner", "both"],
)
async def test_a_write_naming_accounts_is_begin_locks_insert_commit(pool, writer, metadata):
    async with pool.acquire() as conn:
        one = _OneConnection(conn)
        if writer == "store":
            log = PostgresLogStore(one, fence_secret=_SECRET)
        else:
            log = DatabaseLogger({}, fence_secret=_SECRET)
            log.pool = one
        # The first write of each statement also prepares it; count a later one.
        await _log(log, "req-fwp-warm", metadata)

        sent = await _statements_of(conn, _log(log, "req-fwp-counted", metadata))

    assert sent == ["BEGIN", "locks", "fence check and insert", "COMMIT"]
    assert await _logged(pool, "req-fwp-counted")


@pytest.mark.parametrize("writer", ["store", "logger"])
async def test_a_write_naming_no_account_is_one_insert(pool, writer):
    async with pool.acquire() as conn:
        one = _OneConnection(conn)
        if writer == "store":
            log = PostgresLogStore(one, fence_secret=_SECRET)
        else:
            log = DatabaseLogger({}, fence_secret=_SECRET)
            log.pool = one
        await _log(log, "req-fwp-anon-warm", {"route": "/v1/chat/completions"})

        sent = await _statements_of(
            conn, _log(log, "req-fwp-anon", {"route": "/v1/chat/completions"})
        )

    assert sent == ["insert"]
    assert await _logged(pool, "req-fwp-anon")


async def test_a_logged_request_id_is_not_written_twice(pool):
    store = PostgresLogStore(pool, fence_secret=_SECRET)
    for metadata in ({"user_id": _OTHER}, {"route": "/v1/chat/completions"}):
        await _log(store, "req-fwp-twice", metadata)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM api_logs WHERE request_id = 'req-fwp-twice'")
    assert [r["user_id"] for r in rows] == [_OTHER]


async def _until_waiting(pool: Any, key: str) -> None:
    """Return once some session waits for the advisory lock of fence *key*."""
    advisory = fence_account_advisory_key(key)
    async with pool.acquire() as conn:
        for _ in range(400):
            if await conn.fetchval(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND NOT granted AND objsubid = 1 "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND classid::bigint = $1 AND objid::bigint = $2",
                (advisory >> 32) & 0xFFFFFFFF,
                advisory & 0xFFFFFFFF,
            ):
                return
            await asyncio.sleep(0.025)
    raise AssertionError("nothing waited for the lock")


@contextlib.asynccontextmanager
async def _hard_delete_in_progress(pool: Any, account: str) -> Any:
    """A hard delete that has taken its exclusive lock and written the tombstone, uncommitted.

    Yields a function that commits it.
    """
    conn = await pool.acquire()
    transaction = conn.transaction()
    await transaction.start()
    committed = False

    async def _commit() -> None:
        nonlocal committed
        await transaction.commit()
        committed = True

    try:
        await establish_erasure_fence(conn, fence_key=fence_account_digest(account, _SECRET))
        await conn.execute(
            "DELETE FROM api_logs WHERE user_id = $1 OR metadata->>'credential_owner_id' = $1",
            account,
        )
        yield _commit
    finally:
        if not committed:
            await transaction.rollback()
        await pool.release(conn)


@pytest.mark.parametrize("writer", ["store", "logger"])
async def test_a_write_that_waited_for_a_hard_delete_reads_its_tombstone(pool, writer):
    """The write blocks on the hard delete's lock, which commits the tombstone and lets go.

    The fenced INSERT starts after the lock was granted, so it reads the
    tombstone committed while the write waited, and writes nothing.
    """
    if writer == "store":
        log = PostgresLogStore(pool, fence_secret=_SECRET)
    else:
        log = DatabaseLogger({}, fence_secret=_SECRET)
        log.pool = pool
    async with _hard_delete_in_progress(pool, _DELETED) as commit:
        write = asyncio.ensure_future(_log(log, "req-fwp-waited", {"user_id": _DELETED}))
        await _until_waiting(pool, fence_account_digest(_DELETED, _SECRET))
        await commit()
    await asyncio.wait_for(write, 10)

    assert not await _logged(pool, "req-fwp-waited")


async def test_reading_the_fence_in_the_locking_statement_would_miss_the_tombstone(pool):
    """Why the fence is read in the statement after the locks, never in theirs.

    A READ COMMITTED statement reads with the snapshot it took when it started:
    one that waits for the lock and then reads the fence sees it as it was
    before the hard delete it waited for committed.
    """
    key = fence_account_digest(_DELETED, _SECRET)

    async def _lock_and_read_in_one_statement() -> bool:
        async with pool.acquire() as conn, conn.transaction():
            return await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM erasure_fence WHERE account_digest = $2) "
                "FROM (SELECT pg_advisory_xact_lock_shared($1) OFFSET 0) AS locked",
                fence_account_advisory_key(key),
                key,
            )

    async with _hard_delete_in_progress(pool, _DELETED) as commit:
        one_statement = asyncio.ensure_future(_lock_and_read_in_one_statement())
        await _until_waiting(pool, key)
        await commit()
    missed = not await asyncio.wait_for(one_statement, 10)

    async with pool.acquire() as conn, conn.transaction():
        await lock_fence_keys_shared(conn, [key])
        seen_after = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM erasure_fence WHERE account_digest = $1)", key
        )
    assert missed
    assert seen_after


async def test_the_locking_statement_takes_the_keys_in_the_order_given(pool):
    """The second key's lock is waited for while the first is already held."""
    first, second = sorted(
        fence_account_digest(account, _SECRET) for account in ("u-fwp-a", "u-fwp-b")
    )
    holder = await pool.acquire()
    holding = holder.transaction()
    await holding.start()
    locker = await pool.acquire()
    locking = None
    try:
        await holder.execute("SELECT pg_advisory_xact_lock($1)", fence_account_advisory_key(second))

        async def _lock_both() -> None:
            async with locker.transaction():
                await lock_fence_keys_shared(locker, [first, second])

        locking = asyncio.ensure_future(_lock_both())
        await _until_waiting(pool, second)
        async with pool.acquire() as conn:
            held = await conn.fetch(
                "SELECT classid::bigint AS high, objid::bigint AS low, granted "
                "FROM pg_locks WHERE locktype = 'advisory' AND pid = $1",
                locker.get_server_pid(),
            )
    finally:
        await holding.rollback()
        await pool.release(holder)
        if locking is not None:
            await asyncio.wait_for(locking, 10)
        await pool.release(locker)

    def _key(row: Any) -> int:
        value = (row["high"] << 32) | row["low"]
        return value - (1 << 64) if value >= 1 << 63 else value

    by_key = {_key(row): row["granted"] for row in held}
    assert by_key == {
        fence_account_advisory_key(first): True,
        fence_account_advisory_key(second): False,
    }
