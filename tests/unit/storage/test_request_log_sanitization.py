"""Regression tests for request-log payload sanitization."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.postgres_log import PostgresLogStore


@pytest.mark.asyncio
async def test_postgres_log_request_strips_null_bytes_from_all_serialized_fields() -> None:
    """Null bytes in request-log payloads are removed before DB insert."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    store = PostgresLogStore(pool, store_full_prompts=True)

    await store.log_request(
        request_id="req-1",
        model_id="glm-5.1",
        provider="zhipu",
        prompt=[{"role": "user", "content": "hel\x00lo"}],
        response={"message": {"content": "wor\x00ld"}},
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        latency_ms=12,
        status_code=200,
        error="bad\x00error",
        params={"tools": [{"name": "to\x00ol"}]},
        metadata={"us\x00er_id": "user\x00-1", "nested\x00": {"no\x00te": "n\x00ote"}},
        request_payload={"mess\x00ages": [{"content": "pa\x00yload"}]},
    )

    args = conn.execute.await_args.args
    prompt_str = args[18]
    response_str = args[19]
    request_payload_str = args[20]
    error_str = args[22]
    metadata_str = args[25]
    tools_str = args[26]

    assert "\x00" not in prompt_str
    assert "\x00" not in response_str
    assert "\x00" not in request_payload_str
    assert "\x00" not in error_str
    assert "\x00" not in metadata_str
    assert "\x00" not in tools_str

    assert json.loads(prompt_str)[0]["content"] == "hello"
    assert json.loads(response_str)["message"]["content"] == "world"
    assert json.loads(request_payload_str)["messages"][0]["content"] == "payload"
    assert error_str == "baderror"
    assert json.loads(metadata_str)["user_id"] == "user-1"
    assert json.loads(metadata_str)["nested"]["note"] == "note"
    assert json.loads(tools_str)[0]["name"] == "tool"
