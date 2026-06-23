"""Integration tests against a real PostgreSQL instance."""

from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import Request

from serving.servers.auth import hash_api_key, verify_api_key
from serving.storage.database import DatabaseLogger, calculate_cost
from serving.storage.postgres_log import PostgresLogStore
from serving.storage.postgres_operational import PostgresOperationalStore

if TYPE_CHECKING:
    import asyncpg

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn


async def _truncate_tables(pool: asyncpg.pool.Pool) -> None:
    async with pool.acquire() as conn:
        for table in ("api_logs", "api_keys"):
            exists = await conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
            if exists:
                await conn.execute(f"TRUNCATE TABLE {table}")


@pytest_asyncio.fixture
async def db_logger(pg_dsn: str):
    logger = DatabaseLogger({"dsn": pg_dsn}, store_full_prompts=False)
    await logger.initialize()
    assert logger.pool is not None
    await _truncate_tables(logger.pool)
    try:
        yield logger
    finally:
        assert logger.pool is not None
        await _truncate_tables(logger.pool)
        await logger.cleanup()


@pytest.mark.asyncio
async def test_api_logs_schema_contains_cost_columns(db_logger: DatabaseLogger):
    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'api_logs'
            """
        )
        column_names = {row["column_name"] for row in rows}

    assert {"cache_read_tokens", "cache_write_tokens", "cost_usd"}.issubset(column_names)
    assert "prompt_hash" not in column_names
    assert "response_hash" not in column_names


@pytest.mark.asyncio
async def test_log_request_persists_cost_and_usage(db_logger: DatabaseLogger):
    usage: dict[str, Any] = {
        "prompt_tokens": 1000,
        "completion_tokens": 400,
        "reasoning_tokens": 200,
        "cache_read_tokens": 500,
        "cache_write_tokens": 250,
    }
    pricing = {
        "prompt": "0.15",
        "completion": "1.25",
        "input_cache_reads": "0.05",
        "input_cache_writes": "0.10",
    }
    expected_cost = calculate_cost(usage, pricing)

    await db_logger.log_request(
        request_id="req-integration-1",
        model_id="integration-model",
        provider="gemini",
        prompt=[{"role": "user", "content": "hi"}],
        response={"message": "ok"},
        usage=usage,
        latency_ms=123,
        status_code=200,
        params={},
        metadata={"user_id": "integration-user"},
        pricing=pricing,
    )

    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT prompt_tokens, cache_read_tokens, cache_write_tokens, cost_usd
            FROM api_logs WHERE request_id=$1
            """,
            "req-integration-1",
        )

    assert row is not None
    assert row["prompt_tokens"] == 1000
    assert row["cache_read_tokens"] == 500
    assert row["cache_write_tokens"] == 250
    assert float(row["cost_usd"]) == pytest.approx(expected_cost or 0.0)


@pytest.mark.asyncio
async def test_user_turn_averages(db_logger: DatabaseLogger):
    """Per-user avg turns: detail + bulk average num_turns / num_user_turns.

    AVG ignores the NULL turn counts of non-chat (string-prompt) requests, so a
    user with only non-chat rows reports None, and a user with no rows at all is
    omitted from the bulk result.
    """
    usage: dict[str, Any] = {"prompt_tokens": 1, "completion_tokens": 1}
    pricing = {"prompt": "0", "completion": "0"}

    async def _log(rid: str, user_id: str, prompt: Any) -> None:
        await db_logger.log_request(
            request_id=rid,
            model_id="m",
            provider="p",
            prompt=prompt,
            response={"message": "ok"},
            usage=usage,
            latency_ms=10,
            status_code=200,
            params={},
            metadata={"user_id": user_id},
            pricing=pricing,
        )

    # user A: turns 3 (2 user) then 1 (1 user) -> avg_turns 2.0, avg_user_turns 1.5
    await _log("a1", "u-A", [{"role": "user"}, {"role": "assistant"}, {"role": "user"}])
    await _log("a2", "u-A", [{"role": "user"}])
    # user B: a single non-chat (raw string) request -> NULL turn counts
    await _log("b1", "u-B", "raw completion prompt")

    log_store = PostgresLogStore(db_logger.pool, store_full_prompts=False)

    detail_a = await log_store.get_user_detail_usage("u-A")
    assert detail_a["avg_turns"] == pytest.approx(2.0)
    assert detail_a["avg_user_turns"] == pytest.approx(1.5)

    detail_b = await log_store.get_user_detail_usage("u-B")
    assert detail_b["avg_turns"] is None
    assert detail_b["avg_user_turns"] is None

    bulk = await log_store.get_bulk_user_turn_averages(["u-A", "u-B", "u-missing"])
    assert bulk["u-A"]["avg_turns"] == pytest.approx(2.0)
    assert bulk["u-A"]["avg_user_turns"] == pytest.approx(1.5)
    # u-B has a row, but only NULL turn counts -> averages are None.
    assert bulk["u-B"]["avg_turns"] is None
    # u-missing has no rows at all -> omitted entirely.
    assert "u-missing" not in bulk


@pytest.mark.asyncio
async def test_log_request_normalizes_nested_cached_tokens(db_logger: DatabaseLogger):
    usage: dict[str, Any] = {
        "prompt_tokens": 13528,
        "completion_tokens": 98,
        "total_tokens": 13626,
        "prompt_tokens_details": {"cached_tokens": 13520},
        "completion_tokens_details": {"reasoning_tokens": 17},
    }

    await db_logger.log_request(
        request_id="req-integration-minimax-cache",
        model_id="minimax-m2.1",
        provider="minimax",
        prompt=[{"role": "user", "content": "hi"}],
        response={"message": "ok"},
        usage=usage,
        latency_ms=123,
        status_code=200,
        params={},
        metadata={"user_id": "integration-user"},
        pricing={"prompt": "0.15", "completion": "1.25", "input_cache_reads": "0.05"},
    )

    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT prompt_tokens, completion_tokens, reasoning_tokens, cache_read_tokens
            FROM api_logs WHERE request_id=$1
            """,
            "req-integration-minimax-cache",
        )

    assert row is not None
    assert row["prompt_tokens"] == 13528
    assert row["completion_tokens"] == 98
    assert row["reasoning_tokens"] == 17
    assert row["cache_read_tokens"] == 13520


@pytest.mark.asyncio
async def test_postgres_log_store_normalizes_nested_cached_tokens(db_logger: DatabaseLogger):
    usage: dict[str, Any] = {
        "prompt_tokens": 13528,
        "completion_tokens": 98,
        "total_tokens": 13626,
        "prompt_tokens_details": {"cached_tokens": 13520},
        "completion_tokens_details": {"reasoning_tokens": 17},
    }

    log_store = PostgresLogStore(db_logger.pool, store_full_prompts=False)

    await log_store.log_request(
        request_id="req-postgres-log-store-minimax-cache",
        model_id="minimax-m2.1",
        provider="minimax",
        prompt=[{"role": "user", "content": "hi"}],
        response={"message": "ok"},
        usage=usage,
        latency_ms=123,
        status_code=200,
        params={},
        metadata={"user_id": "integration-user"},
        pricing={"prompt": "0.15", "completion": "1.25", "input_cache_reads": "0.05"},
    )

    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT prompt_tokens, completion_tokens, reasoning_tokens, cache_read_tokens
            FROM api_logs WHERE request_id=$1
            """,
            "req-postgres-log-store-minimax-cache",
        )

    assert row is not None
    assert row["prompt_tokens"] == 13528
    assert row["completion_tokens"] == 98
    assert row["reasoning_tokens"] == 17
    assert row["cache_read_tokens"] == 13520


@pytest.mark.asyncio
async def test_postgres_log_store_routewise_bootstrap_rows_are_normalized(
    db_logger: DatabaseLogger,
):
    assert db_logger.pool is not None
    now = dt.datetime.now(dt.timezone.utc)
    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_logs (
                timestamp, request_id, model_id, provider,
                ttft_ms, latency_ms, status_code, error,
                prompt_tokens, completion_tokens, metadata
            )
            VALUES
                ($1, 'rw-bootstrap-newer', 'm', 'provider-a',
                 200, 500, 200, NULL, 1000, 100,
                 $2::jsonb),
                ($3, 'rw-bootstrap-older', 'm', 'provider-b',
                 NULL, 700, 500, 'boom', 1000, 0,
                 $4::jsonb),
                ($5, 'rw-bootstrap-too-old-for-limit', 'm', 'provider-old',
                 900, 1000, 200, NULL, 1000, 100,
                 $6::jsonb),
                ($7, 'rw-bootstrap-synthetic', 'm', 'provider-synthetic',
                 150, 400, 200, NULL, 1000, 100,
                 $8::jsonb)
            """,
            now,
            json.dumps({"endpoint_id": "m:provider-a"}),
            now - dt.timedelta(seconds=10),
            json.dumps(
                {
                    "routewise": {"primary_provider": "m:provider-b"},
                    "failed_attempts": [
                        {
                            "endpoint_id": "m:provider-c",
                            "error_type": "timeout",
                            "error": "deadline",
                        }
                    ],
                }
            ),
            now - dt.timedelta(seconds=20),
            json.dumps({"endpoint_id": "m:provider-old"}),
            # Synthetic probe row: must be excluded from the bootstrap so it
            # cannot skew replayed latency profiles / cost envelope.
            now - dt.timedelta(seconds=5),
            json.dumps({"endpoint_id": "m:provider-synthetic", "synthetic_probe": True}),
        )

    log_store = PostgresLogStore(db_logger.pool, store_full_prompts=False)
    rows = await log_store.get_routewise_bootstrap_rows(
        model_ids=["m"],
        since=now - dt.timedelta(minutes=1),
        limit=2,
    )

    assert [row["endpoint_id"] for row in rows] == ["m:provider-b", "m:provider-a"]
    assert rows[0]["failed_attempts"] == (
        {
            "endpoint_id": "m:provider-c",
            "error_type": "timeout",
            "error": "deadline",
        },
    )

    all_rows = await log_store.get_routewise_bootstrap_rows(
        model_ids=["m"],
        since=now - dt.timedelta(minutes=1),
        limit=None,
    )
    assert [row["endpoint_id"] for row in all_rows] == [
        "m:provider-old",
        "m:provider-b",
        "m:provider-a",
    ]
    # The synthetic probe row is filtered out of the bootstrap entirely.
    assert "m:provider-synthetic" not in [row["endpoint_id"] for row in all_rows]


@pytest.mark.asyncio
async def test_postgres_log_store_accepts_routewise_metadata_with_nonfinite_values(
    db_logger: DatabaseLogger,
):
    log_store = PostgresLogStore(db_logger.pool, store_full_prompts=False)

    await log_store.log_request(
        request_id="req-postgres-log-store-routewise-inf",
        model_id="minimax-fast",
        provider="minimax",
        prompt=[{"role": "user", "content": "hi"}],
        response={"message": "ok"},
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        latency_ms=123,
        status_code=200,
        params={},
        metadata={
            "user_id": "integration-user",
            "routewise": {
                "selected_provider_type": "on_demand",
                "v_t": 0.01,
                "gain_c": float("-inf"),
                "gain_q": float("-inf"),
                "gain_a": 0.0,
                "theta_q": None,
            },
        },
        pricing={"prompt": "0.15", "completion": "1.25"},
    )

    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_id, metadata
            FROM api_logs WHERE request_id=$1
            """,
            "req-postgres-log-store-routewise-inf",
        )

    assert row is not None
    assert row["user_id"] == "integration-user"
    assert row["metadata"]["routewise"]["selected_provider_type"] == "on_demand"
    assert row["metadata"]["routewise"]["gain_c"] is None
    assert row["metadata"]["routewise"]["gain_q"] is None


@pytest.mark.asyncio
async def test_db_logger_accepts_routewise_metadata_with_nonfinite_values(
    db_logger: DatabaseLogger,
):
    await db_logger.log_request(
        request_id="req-db-logger-routewise-inf",
        model_id="minimax-fast",
        provider="minimax",
        prompt=[{"role": "user", "content": "hi"}],
        response={"message": "ok", "score": float("inf")},
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        latency_ms=123,
        status_code=200,
        params={"tools": [{"type": "function", "score": float("nan")}]},
        metadata={
            "user_id": "integration-user",
            "routewise": {
                "selected_provider_type": "on_demand",
                "v_t": 0.01,
                "gain_c": float("-inf"),
                "gain_q": float("-inf"),
                "gain_a": 0.0,
                "theta_q": None,
            },
        },
        pricing={"prompt": "0.15", "completion": "1.25"},
        request_payload={"threshold": float("inf")},
    )

    assert db_logger.pool is not None
    async with db_logger.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_id, metadata, response, request_payload, tools
            FROM api_logs WHERE request_id=$1
            """,
            "req-db-logger-routewise-inf",
        )

    assert row is not None
    assert row["user_id"] == "integration-user"
    assert row["metadata"]["routewise"]["selected_provider_type"] == "on_demand"
    assert row["metadata"]["routewise"]["gain_c"] is None
    assert row["metadata"]["routewise"]["gain_q"] is None
    assert row["response"] is not None
    assert '"score": null' in row["response"]
    assert row["request_payload"]["threshold"] is None
    assert row["tools"][0]["score"] is None


@pytest.mark.asyncio
async def test_verify_api_key_against_real_database(db_logger: DatabaseLogger, monkeypatch):
    assert db_logger.pool is not None
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "integration-secret")

    plaintext_key = "hyi-integration-key"
    key_hash = hash_api_key(plaintext_key)
    key_prefix = plaintext_key[:12]
    user_id = "integration-user"

    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO api_keys (
                key_hash, key_prefix, user_id, user_name, quota_daily_cost_usd
            ) VALUES ($1, $2, $3, $4, $5)
            """,
            key_hash,
            key_prefix,
            user_id,
            "Integration User",
            Decimal("1000.00"),
        )

    pricing = {"prompt": "0.15", "completion": "1.25"}
    usage = {"prompt_tokens": 400, "completion_tokens": 200}
    await db_logger.log_request(
        request_id="req-integration-2",
        model_id="integration-model",
        provider="gemini",
        prompt=[{"role": "user", "content": "ping"}],
        response={"message": "pong"},
        usage=usage,
        latency_ms=45,
        status_code=200,
        params={},
        metadata={"user_id": user_id},
        pricing=pricing,
    )

    # Build store abstractions from the pool
    op_store = PostgresOperationalStore(db_logger.pool)
    await op_store.initialize()
    log_store = PostgresLogStore(db_logger.pool, store_full_prompts=False)

    # Increment the daily cost counter (verify_api_key reads from user_daily_cost,
    # not api_logs)
    cost = calculate_cost(usage, pricing) or 0.0
    await op_store.increment_user_cost(user_id, cost)

    mock_request = MagicMock(spec=Request)
    result = await verify_api_key(
        request=mock_request,
        authorization=f"Bearer {plaintext_key}",
        op_store=op_store,
        log_store=log_store,
    )

    assert result["user_id"] == user_id
    assert result["authenticated"] is True
    assert result["quota_remaining_cost_usd"] < 1000
