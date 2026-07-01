"""Unit tests for per-provider model activity aggregation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.database import DatabaseLogger
from serving.storage.postgres_log import PostgresLogStore


@pytest.fixture
def pg_conn():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    return conn


@pytest.fixture
def pg_pool(pg_conn):
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield pg_conn

    pool.acquire = _acquire
    return pool


def _activity_row(provider: str, *, model_id: str = "glm-5.2") -> dict:
    ts = datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc)
    return {
        "model_id": model_id,
        "provider": provider,
        "request_count": 3,
        "success_count": 2,
        "last_request_at": ts,
        "avg_latency_ms": 123.4,
        "stream_count": 1,
        "non_stream_count": 2,
        "stream_success_count": 1,
        "non_stream_success_count": 1,
        "stream_last_success_at": ts,
        "non_stream_last_success_at": ts,
        "stream_avg_ttft_ms": 45.6,
        "avg_completion_tokens": 78.9,
        "stream_avg_completion_tokens": 80.1,
        "non_stream_avg_completion_tokens": 77.7,
    }


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda pool: PostgresLogStore(pool),
        lambda pool: _database_logger(pool),
    ],
)
async def test_get_model_activity_excludes_synthetic_provider_labels(
    store_factory,
    pg_pool,
    pg_conn,
):
    pg_conn.fetch.return_value = [
        _activity_row("router"),
        _activity_row(""),
        _activity_row("zai"),
    ]

    store = store_factory(pg_pool)
    result = await store.get_model_activity(window_minutes=15)

    assert set(result) == {"glm-5.2::zai"}
    sql = pg_conn.fetch.call_args[0][0]
    assert "provider NOT IN ('', 'router')" in sql


def _database_logger(pool) -> DatabaseLogger:
    logger = DatabaseLogger({}, store_full_prompts=False)
    logger.pool = pool
    return logger
