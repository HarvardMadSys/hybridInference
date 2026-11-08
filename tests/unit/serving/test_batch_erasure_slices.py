"""A user's erasure deletes their batches a slice at a time (M28).

One DELETE of every batch, cascading to tens of thousands of large results,
can outlast the statement timeout, and then undoes everything it did on every
retry. ``delete_user_batches`` deletes each batch's items a bounded slice per
statement, each slice a transaction of its own under the user's admission lock
and the batch's row lock, then the batch. The connection here records what it
is asked to run.
"""

from __future__ import annotations

import contextlib
from typing import Any

from serving.storage import batch_store
from serving.storage.batch_store import BatchStore


class RecordingConn:
    """Enough of an asyncpg connection to follow an erasure, transaction by transaction."""

    def __init__(self, items: dict[str, int]) -> None:
        self.items = dict(items)
        self.transactions = 0
        self.current: int | None = None
        self.log: list[tuple[int | None, str, tuple[Any, ...]]] = []

    @contextlib.asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        outer, self.current = self.current, self.transactions
        try:
            yield
        finally:
            self.current = outer

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.log.append((self.current, sql, args))
        if sql.startswith("SELECT id FROM batch_jobs"):
            return min(self.items) if self.items else None
        if "FOR UPDATE" in sql:
            return args[0] in self.items or None
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: Any) -> str:
        self.log.append((self.current, sql, args))
        if sql.startswith("DELETE FROM batch_items"):
            batch_id, limit = args
            taken = min(self.items[batch_id], limit)
            self.items[batch_id] -= taken
            return f"DELETE {taken}"
        if sql.startswith("DELETE FROM batch_jobs"):
            self.items.pop(args[0])
            return "DELETE 1"
        return "DELETE 0"


class Pool:
    def __init__(self, conn: RecordingConn) -> None:
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_each_slice_of_items_is_a_transaction_of_its_own(monkeypatch):
    monkeypatch.setattr(batch_store, "_ERASE_ITEMS_PER_STATEMENT", 3)
    conn = RecordingConn({"batch_a": 7, "batch_b": 2})

    assert await BatchStore(Pool(conn)).delete_user_batches("alice") == 2

    item_deletes = [
        (tx, args) for tx, sql, args in conn.log if sql.startswith("DELETE FROM batch_items")
    ]
    # Seven items in slices of three, then two: each slice in its own transaction.
    assert [args for _, args in item_deletes] == [
        ("batch_a", 3),
        ("batch_a", 3),
        ("batch_a", 3),
        ("batch_b", 3),
    ]
    assert len({tx for tx, _ in item_deletes}) == len(item_deletes)
    assert None not in {tx for tx, _ in item_deletes}
    # Every slice holds the user's admission lock and the batch's row first.
    for tx, _ in item_deletes:
        statements = [sql for t, sql, _ in conn.log if t == tx]
        assert statements[0] == batch_store._CLAIM_LOCK_SQL
        assert "FOR UPDATE" in statements[1]
    assert conn.items == {}


async def test_an_erasure_run_again_goes_on_from_where_it_stopped(monkeypatch):
    monkeypatch.setattr(batch_store, "_ERASE_ITEMS_PER_STATEMENT", 2)
    conn = RecordingConn({"batch_a": 5})
    calls = 0
    execute = conn.execute

    async def failing_once(sql: str, *args: Any) -> str:
        nonlocal calls
        if sql.startswith("DELETE FROM batch_items"):
            calls += 1
            if calls == 2:
                raise TimeoutError("statement timeout")
        return await execute(sql, *args)

    conn.execute = failing_once
    store = BatchStore(Pool(conn))
    with contextlib.suppress(TimeoutError):
        await store.delete_user_batches("alice")
    # The first slice stayed deleted.
    assert conn.items == {"batch_a": 3}

    assert await store.delete_user_batches("alice") == 1
    assert conn.items == {}
