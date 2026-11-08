"""The Responses store's retention purge and stored-size report, without a database.

``tests/integration/storage/test_postgres_responses_store.py`` runs the purge
against PostgreSQL.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from serving.storage import responses_store
from serving.storage.responses_store import ResponseStore


class _Conn:
    def __init__(self, *, deleted: list[int] | None = None, row: Any = None) -> None:
        self.deleted = list(deleted or [])
        self.row = row
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return f"DELETE {self.deleted.pop(0)}"

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self.row


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self) -> Any:
        conn = self.conn

        class _Acquired:
            async def __aenter__(self) -> _Conn:
                return conn

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Acquired()


async def test_the_purge_deletes_in_batches_until_one_comes_back_short():
    conn = _Conn(deleted=[2, 2, 1])

    assert await responses_store.purge_expired(_Pool(conn), batch=2) == 5

    assert [args for _, args in conn.executed] == [(30, 2)] * 3


async def test_the_purge_keeps_responses_for_30_days_by_default():
    conn = _Conn(deleted=[0])

    assert await responses_store.purge_expired(_Pool(conn)) == 0

    assert conn.executed == [(responses_store._PURGE_SQL, (30, responses_store.PURGE_BATCH))]
    assert responses_store.RETENTION_DAYS == 30


async def test_the_hourly_job_never_raises(caplog):
    class _Broken:
        def acquire(self) -> Any:
            raise RuntimeError("pool closed")

    await responses_store.retention_job(_Broken())

    assert "responses_retention failed" in caplog.text


def test_the_job_is_registered_hourly_clear_of_the_rollups():
    class _Scheduler:
        def __init__(self) -> None:
            self.jobs: list[dict[str, Any]] = []

        def add_job(self, func: Any, **kwargs: Any) -> None:
            self.jobs.append({"func": func, **kwargs})

    scheduler = _Scheduler()
    pool = object()
    responses_store.register_retention_job(scheduler, pool)

    (job,) = scheduler.jobs
    assert job["func"] is responses_store.retention_job
    assert job["args"] == [pool]
    assert job["id"] == "responses_retention"
    assert (job["replace_existing"], job["coalesce"], job["max_instances"]) == (True, True, 1)
    assert str(job["trigger"].fields[6]) == "40"  # minute


@pytest.mark.parametrize(
    "stored",
    [
        '[{"role": "user", "content": "héllo ✓"}]',
        b'[{"role": "user", "content": "hi"}]',
        [{"role": "user", "content": "decoded ✓"}],
    ],
    ids=["text", "bytes", "decoded"],
)
async def test_a_read_reports_the_stored_conversations_size_in_bytes(stored):
    row = {
        "id": "resp_1",
        "user_id": "u",
        "response": "{}",
        "messages": stored,
        "previous_response_id": None,
        "model": "m",
    }

    got = await ResponseStore(_Pool(_Conn(row=row))).get("resp_1")

    if isinstance(stored, str):
        expected = len(stored.encode())
    elif isinstance(stored, bytes):
        expected = len(stored)
    else:
        expected = len(json.dumps(stored).encode())
    assert got["messages_bytes"] == expected
