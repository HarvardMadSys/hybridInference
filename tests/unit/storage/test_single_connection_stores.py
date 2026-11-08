"""Stores bound to one connection, as a configuration restore binds them to its transaction."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.admin import config_backup
from serving.storage.batch_store import BatchStore
from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.pools import SingleConnectionPool
from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = pytest.mark.asyncio


def _connection() -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="DELETE 1")
    conn.fetchrow = AsyncMock(return_value=None)
    return conn


async def test_every_acquire_yields_the_one_connection() -> None:
    conn = _connection()
    pool = SingleConnectionPool(conn)

    async with pool.acquire() as first, pool.acquire(timeout=1) as nested:
        assert first is conn
        assert nested is conn


async def test_the_operational_store_runs_every_call_on_the_bound_connection() -> None:
    general, request = MagicMock(), MagicMock()
    conn = _connection()
    store = CachedOperationalStore(
        PostgresOperationalStore(general, request_pool=request), InMemoryCache()
    )

    bound = store.on_connection(conn)

    assert isinstance(bound, PostgresOperationalStore)
    await bound.set_setting("signup_enabled", "true", "bool", "admin")
    # A request-path lookup, which reads the request pool, uses it too.
    assert await bound.get_setting("signup_enabled") is None
    assert conn.execute.await_count == 1
    assert conn.fetchrow.await_count == 1
    general.acquire.assert_not_called()
    request.acquire.assert_not_called()


async def test_the_batch_store_runs_on_the_bound_connection() -> None:
    pool = MagicMock()
    conn = _connection()

    assert await BatchStore(pool).on_connection(conn).delete_model_limit("glm") is True

    conn.execute.assert_awaited_once()
    pool.acquire.assert_not_called()


class _Transaction:
    def __init__(self, conn: object) -> None:
        self.conn = conn

    @contextlib.asynccontextmanager
    async def connection(self):
        yield self.conn


async def test_a_restore_binds_both_stores_to_the_transaction() -> None:
    conn = _connection()
    op_store = SimpleNamespace(on_connection=lambda c: ("op", c))
    batch_store = SimpleNamespace(on_connection=lambda c: ("batch", c))

    async with config_backup._stores_in(_Transaction(conn), op_store, batch_store) as bound:
        assert bound == (("op", conn), ("batch", conn))

    async with config_backup._stores_in(_Transaction(conn), op_store, None) as bound:
        assert bound == (("op", conn), None)


async def test_a_store_that_cannot_join_the_transaction_refuses_the_restore() -> None:
    """Writing it on a connection of its own is the partial restore this prevents."""
    with pytest.raises(RuntimeError, match="nothing was restored"):
        async with config_backup._stores_in(_Transaction(_connection()), object(), None):
            pass


async def test_an_in_memory_transaction_keeps_the_stores_it_was_given() -> None:
    op_store, batch_store = object(), object()

    async with config_backup._stores_in(object(), op_store, batch_store) as bound:
        assert bound == (op_store, batch_store)
