"""Tests for the served-model / served-endpoint columns on api_logs inserts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.database import DatabaseLogger
from serving.storage.postgres_log import PostgresLogStore

# conn.execute is called as (sql, $1, $2, ...), so args[0] is the SQL string and
# placeholder $N maps to args[N]. served_model_id is $34, served_endpoint_id $35.
MODEL_ID_ARG = 2
SERVED_MODEL_ARG = 34
SERVED_ENDPOINT_ARG = 35


def _store_with_capture():
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm
    return PostgresLogStore(pool, store_full_prompts=True), conn


async def _log(store, **overrides):
    base = {
        "request_id": "req-1",
        "model_id": "glm-5.1",
        "provider": "zhipu",
        "prompt": [{"role": "user", "content": "hi"}],
        "response": {"message": {"content": "ok"}},
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "latency_ms": 10,
        "status_code": 200,
    }
    base.update(overrides)
    await store.log_request(**base)


@pytest.mark.asyncio
async def test_served_model_defaults_to_model_id_and_endpoint_from_metadata():
    store, conn = _store_with_capture()
    await _log(store, metadata={"endpoint_id": "zhipu:api.z.ai:443"})
    args = conn.execute.await_args.args
    assert args[SERVED_MODEL_ARG] == "glm-5.1"  # defaults to model_id
    assert args[SERVED_ENDPOINT_ARG] == "zhipu:api.z.ai:443"


@pytest.mark.asyncio
async def test_served_model_id_override_is_recorded():
    """When the served model diverges (alias/reroute), it is logged distinctly."""
    store, conn = _store_with_capture()
    await _log(
        store,
        model_id="minimax-m3",
        served_model_id="qwen3.6-35b",
        metadata={"endpoint_id": "sglang:gpu-1:8000"},
    )
    args = conn.execute.await_args.args
    assert args[MODEL_ID_ARG] == "minimax-m3"  # requested model_id is unchanged
    assert args[SERVED_MODEL_ARG] == "qwen3.6-35b"  # served model recorded
    assert args[SERVED_ENDPOINT_ARG] == "sglang:gpu-1:8000"


@pytest.mark.asyncio
async def test_served_endpoint_falls_back_to_base_url_then_provider():
    store, conn = _store_with_capture()
    # No endpoint_id -> base_url
    await _log(store, metadata={"base_url": "https://api.example.test"})
    args = conn.execute.await_args.args
    assert args[SERVED_ENDPOINT_ARG] == "https://api.example.test"

    # No endpoint_id, no base_url -> provider
    store2, conn2 = _store_with_capture()
    await _log(store2, provider="minimax", metadata={})
    args2 = conn2.execute.await_args.args
    assert args2[SERVED_ENDPOINT_ARG] == "minimax"


@pytest.mark.asyncio
async def test_create_tables_migrates_served_columns():
    """DatabaseLogger._create_tables (the schema path that actually runs at
    startup) must add the served_model_id / served_endpoint_id columns that
    PostgresLogStore.log_request inserts into. Without this migration, every
    request-log insert fails with UndefinedColumnError on an existing DB.
    """
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="")
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    logger_ = DatabaseLogger({}, store_full_prompts=True)
    logger_.pool = pool
    await logger_._create_tables()

    executed_sql = " ".join(
        call.args[0] for call in conn.execute.await_args_list if call.args
    )
    assert "ADD COLUMN IF NOT EXISTS served_model_id" in executed_sql
    assert "ADD COLUMN IF NOT EXISTS served_endpoint_id" in executed_sql
