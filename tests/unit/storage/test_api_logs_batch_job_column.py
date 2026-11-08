"""``api_logs.batch_job_id``: attribution of a row to the batch that issued it.

Both insert paths (``PostgresLogStore`` and the boot-time ``DatabaseLogger``)
write ``metadata["batch_job_id"]`` into its own column, exactly as they write
``agent_job_id``. Unlike ``agent_job_id`` the column has no index: nothing reads
``api_logs`` by batch.

``conn.execute`` is called as ``(sql, $1, $2, ...)``, so placeholder ``$N`` is
``args[N]``: ``agent_job_id`` is ``$36`` and ``batch_job_id`` ``$37``, the last.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.database import DatabaseLogger
from serving.storage.postgres_log import PostgresLogStore

AGENT_JOB_ARG = 36
BATCH_JOB_ARG = 37


def _capturing_pool() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return pool, conn


def _postgres_log_store() -> tuple[object, MagicMock]:
    pool, conn = _capturing_pool()
    return PostgresLogStore(pool, store_full_prompts=False), conn


def _database_logger() -> tuple[object, MagicMock]:
    pool, conn = _capturing_pool()
    logger = DatabaseLogger({}, store_full_prompts=False)
    logger.pool = pool
    return logger, conn


async def _log(writer: object, metadata: dict[str, str]) -> None:
    await writer.log_request(  # type: ignore[attr-defined]
        request_id="req-1",
        model_id="glm-5.1",
        provider="zhipu",
        prompt=[{"role": "user", "content": "hi"}],
        response={"message": "ok"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata=metadata,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("make_writer", [_postgres_log_store, _database_logger])
async def test_a_batch_rows_job_lands_in_its_own_column(make_writer):
    writer, conn = make_writer()

    await _log(writer, {"user_id": "u-1", "agent_job_id": "job-1", "batch_job_id": "batch_1"})

    sql, *_ = args = conn.execute.await_args.args
    assert "agent_job_id, batch_job_id" in sql
    assert "$36, $37" in sql
    assert len(args) == BATCH_JOB_ARG + (3 if make_writer is _postgres_log_store else 1)
    assert args[AGENT_JOB_ARG] == "job-1"
    assert args[BATCH_JOB_ARG] == "batch_1"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_writer", [_postgres_log_store, _database_logger])
async def test_ordinary_traffic_leaves_the_column_null(make_writer):
    writer, conn = make_writer()

    await _log(writer, {"user_id": "u-1"})

    assert conn.execute.await_args.args[BATCH_JOB_ARG] is None
