"""Stored Responses and cost counters check the erasure fence, as ``api_logs`` writes do.

A stream still running when its account is hard-deleted ends after the purge:
its Responses save and its cost increment must not write the erased account's
rows back. With a fence secret, each takes the account's shared fence lock in
one statement and writes only where no tombstone covers the account, in the
next. ``tests/integration/storage/test_erasure_fence_writers_postgres.py`` runs
the protocol against PostgreSQL.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage import postgres_operational, responses_store
from serving.storage.base import CostIncrement
from serving.storage.erasure_fence import fence_account_advisory_key, fence_account_digest
from serving.storage.postgres_operational import (
    COST_INCREMENTS_FENCED_SQL,
    COST_INCREMENTS_SQL,
    PostgresOperationalStore,
)
from serving.storage.responses_store import ResponseStore

pytestmark = pytest.mark.asyncio

SECRET = "fence-secret-for-unit-tests"


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.in_transaction = False

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return 4242 if "txid_current" in sql else False

    @contextlib.asynccontextmanager
    async def _transaction(self) -> Any:
        self.in_transaction = True
        yield
        self.in_transaction = False

    def transaction(self) -> Any:
        return self._transaction()


def _pool(conn: _Conn) -> Any:
    pool = MagicMock()

    @contextlib.asynccontextmanager
    async def acquire() -> Any:
        yield conn

    pool.acquire = acquire
    return pool


async def _save(store: ResponseStore, user_id: str = "u-erased") -> None:
    await store.save(
        response_id="resp_1",
        user_id=user_id,
        response={"id": "resp_1"},
        messages=[{"role": "user", "content": "hi"}],
    )


async def test_a_save_locks_the_owners_fence_key_then_writes_only_without_a_tombstone():
    conn = _Conn()

    await _save(ResponseStore(_pool(conn), fence_secret=SECRET))

    (lock_sql, lock_args), (save_sql, save_args) = conn.calls
    digest = fence_account_digest("u-erased", SECRET)
    assert "pg_advisory_xact_lock_shared" in lock_sql
    assert lock_args == ([fence_account_advisory_key(digest)],)
    assert save_sql is responses_store._FENCED_SAVE_SQL
    assert "FROM erasure_fence WHERE account_digest = ANY($7::text[])" in save_sql
    assert save_args[0] == "resp_1"
    assert save_args[-1] == [digest]


async def test_without_a_fence_secret_a_save_is_the_plain_write():
    conn = _Conn()

    await _save(ResponseStore(_pool(conn)))

    ((sql, args),) = conn.calls
    assert sql is responses_store._SAVE_SQL
    assert len(args) == 6


async def test_a_fenced_save_is_reported(caplog):
    conn = _Conn()
    conn.fetchval = AsyncMock(return_value=True)  # the tombstone was there

    await _save(ResponseStore(_pool(conn), fence_secret=SECRET))

    assert "not stored: its account was erased" in caplog.text


def _increments(*users: str) -> list[CostIncrement]:
    return [CostIncrement(user, "2026-11-08", None, Decimal("0.5"), 1) for user in users]


async def test_a_cost_write_locks_every_accounts_fence_key_and_leaves_erased_ones_out():
    conn = _Conn()
    store = PostgresOperationalStore(_pool(conn), fence_secret=SECRET)

    await store.increment_user_costs(_increments("u-b", "u-a", "u-b"))

    (lock_sql, lock_args), (sql, args) = conn.calls
    digests = {user: fence_account_digest(user, SECRET) for user in ("u-a", "u-b")}
    assert "pg_advisory_xact_lock_shared" in lock_sql
    # Each account once, in the one order every writer takes them in.
    assert lock_args == ([fence_account_advisory_key(d) for d in sorted(digests.values())],)
    assert sql is COST_INCREMENTS_FENCED_SQL
    assert args[7] == [digests["u-b"], digests["u-a"], digests["u-b"]]
    assert "WHERE NOT EXISTS (SELECT 1 FROM erasure_fence" in sql


async def test_a_huge_batch_checks_the_fence_in_its_statement_without_the_locks(monkeypatch):
    monkeypatch.setattr(postgres_operational, "_COST_FENCE_LOCKS_MAX", 2)
    conn = _Conn()
    store = PostgresOperationalStore(_pool(conn), fence_secret=SECRET)

    await store.increment_user_costs(_increments("u-a", "u-b", "u-c"))

    ((sql, args),) = conn.calls
    assert sql is COST_INCREMENTS_FENCED_SQL
    assert len(args[7]) == 3


async def test_without_a_fence_secret_the_cost_write_is_unchanged():
    conn = _Conn()

    await PostgresOperationalStore(_pool(conn)).increment_user_costs(_increments("u-a"))

    ((sql, args),) = conn.calls
    assert sql is COST_INCREMENTS_SQL
    assert len(args) == 7


async def test_a_store_on_one_connection_keeps_the_fence():
    store = PostgresOperationalStore(_pool(_Conn()), fence_secret=SECRET)

    assert store.on_connection(MagicMock())._fence_secret == SECRET


def test_the_fenced_cost_statement_writes_what_the_plain_one_writes():
    """Only the batch's rows differ: the counter writes are the same text."""
    plain_writes = COST_INCREMENTS_SQL.split("),", 1)[1]
    fenced_writes = COST_INCREMENTS_FENCED_SQL.split("),", 1)[1]
    assert plain_writes == fenced_writes
