"""The batch sweep's steps fail on their own (M23).

A sweep that failed in one step -- a purge past the statement timeout, say --
skipped every step after it, in the store and in the scheduler: the oldest
batch comes first in every sweep, so it could stop file collection, the
settling of abandoned validations and webhook cleanup for good.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from serving.batch.config import BatchConfig
from serving.batch.scheduler import BatchScheduler
from serving.storage import batch_store
from serving.storage.batch_store import BatchStore


class _Conn:
    """A connection on which the purge's search fails; everything else succeeds."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        if sql == batch_store._PURGE_CANDIDATES_SQL:
            raise TimeoutError("canceling statement due to statement timeout")
        self.sent.append(sql)
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.sent.append(" ".join(sql.split()))
        return "DELETE 0"


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_a_failing_purge_leaves_the_rest_of_the_store_sweep_to_run():
    conn = _Conn()
    counts = await BatchStore(_Pool(conn)).sweep(retention_days=7)
    assert counts == {"expired": 0, "finalized": 0, "purged": 0}
    assert "DELETE FROM batch_read_leases WHERE expires_at <= now()" in conn.sent
    assert any(sql.startswith("DELETE FROM batch_interactive_load") for sql in conn.sent)


async def test_a_failing_store_sweep_leaves_the_rest_of_the_scheduler_sweep_to_run():
    store = SimpleNamespace(
        sweep=AsyncMock(side_effect=TimeoutError("statement timeout")),
        stale_validations=AsyncMock(return_value=["batch_v"]),
        fail_validation=AsyncMock(),
        seal_backlog=AsyncMock(return_value=0),
    )
    files = SimpleNamespace(collect_garbage=AsyncMock(return_value={"files_collected": 1}))
    webhooks = SimpleNamespace(fail_exhausted=AsyncMock(), purge_old_events=AsyncMock())
    scheduler = BatchScheduler(
        app=None,
        services=SimpleNamespace(),
        store=store,
        limits=None,
        file_store=files,
        webhook_store=webhooks,
    )

    await scheduler._sweep(BatchConfig())

    files.collect_garbage.assert_awaited_once()
    store.fail_validation.assert_awaited_once()
    webhooks.fail_exhausted.assert_awaited_once()
    webhooks.purge_old_events.assert_awaited_once()
