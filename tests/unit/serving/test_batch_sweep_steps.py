"""The batch sweep's steps, and the batches within a step, fail on their own (M23).

A sweep that failed in one step -- a purge past the statement timeout, say --
skipped every step after it, in the store and in the scheduler: the oldest
batch comes first in every sweep, so it could stop file collection, the
settling of abandoned validations and webhook cleanup for good. Within a step,
one batch that fails would likewise stop the step for every batch after it.
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


class _TwoBatches(_Conn):
    """Two batches overdue and then due to be finalized, in the same order; none to purge."""

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        if sql == batch_store._PURGE_CANDIDATES_SQL:
            return []
        return [{"id": "batch_a"}, {"id": "batch_b"}]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return "in_progress"


async def test_a_batch_a_step_fails_on_does_not_stop_the_step_for_the_next(monkeypatch):
    """The batches come in the same order every sweep: the first must not stop the rest."""
    settled: list[tuple[str, str]] = []

    async def settle(self: BatchStore, conn: Any, batch_id: str, status: str) -> str:
        settled.append((batch_id, status))
        if batch_id == "batch_a":
            raise TimeoutError("canceling statement due to statement timeout")
        return "completed"

    monkeypatch.setattr(BatchStore, "_settle", settle)

    counts = await BatchStore(_Pool(_TwoBatches())).sweep(retention_days=7)

    assert settled == [
        ("batch_a", "expired"),
        ("batch_b", "expired"),
        ("batch_a", "in_progress"),
        ("batch_b", "in_progress"),
    ]
    assert counts == {"expired": 1, "finalized": 1, "purged": 0}


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
