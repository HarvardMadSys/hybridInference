"""Unit tests for the batch store's query shapes and row mapping.

The pool is mocked, so these assert the SQL the store issues (ownership
scoping, cascade delete) and the JSON decoding, not Postgres behaviour.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.batch_store import BatchStore


@pytest.fixture
def conn() -> MagicMock:
    mock = MagicMock()
    mock.execute = AsyncMock(return_value="INSERT 0 1")
    mock.executemany = AsyncMock()
    mock.fetchrow = AsyncMock(return_value=None)
    mock.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _tx():
        yield

    mock.transaction = _tx
    return mock


@pytest.fixture
def store(conn: MagicMock) -> BatchStore:
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return BatchStore(pool)


async def test_create_batch_inserts_job_then_items(store, conn) -> None:
    await store.create_batch(
        batch_id="batch_1",
        user_id="u1",
        role="free",
        endpoint="/v1/chat/completions",
        items=[
            {
                "custom_id": "a",
                "endpoint": "/v1/chat/completions",
                "model_id": "m",
                "request": {"model": "m"},
            },
        ],
        model_ids=["m"],
        metadata={"x": 1},
    )
    job_sql = conn.execute.await_args.args[0]
    assert "INSERT INTO batch_jobs" in job_sql
    conn.executemany.assert_awaited_once()


async def test_delete_batch_scopes_to_owner(store, conn) -> None:
    conn.execute = AsyncMock(return_value="DELETE 1")
    deleted = await store.delete_batch("batch_1", user_id="u1")
    assert deleted is True
    sql = conn.execute.await_args.args[0]
    assert "AND user_id" in sql


async def test_get_batch_decodes_json_columns(store, conn) -> None:
    conn.fetchrow = AsyncMock(
        return_value={
            "id": "batch_1",
            "user_id": "u1",
            "role": "free",
            "endpoint": "/v1/chat/completions",
            "status": "in_progress",
            "model_ids": '["m1", "m2"]',
            "request_count": 2,
            "completed_count": 0,
            "failed_count": 0,
            "metadata": '{"k": "v"}',
            "error": None,
        }
    )
    row = await store.get_batch("batch_1")
    assert row is not None
    assert row["model_ids"] == ["m1", "m2"]
    assert row["metadata"] == {"k": "v"}


async def test_list_active_batches_filters_terminal_statuses(store, conn) -> None:
    await store.list_active_batches()
    sql = conn.fetch.await_args.args[0]
    assert "cancelling" in sql
    assert "completed" not in sql.split("WHERE", 1)[1]
