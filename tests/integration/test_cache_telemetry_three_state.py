"""Three-state cache accounting in provider observability, against real Postgres.

A provider that reports no cache usage must not be scored as though it missed
every lookup. These drive the real ``/admin/api/provider-observability`` route
against real ``api_logs`` rows so the SQL predicates are exercised, not a canned
fake pool.

States (identical positive prompt tokens): NULL = unknown, 0 = measured miss,
900 = measured hit.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices, get_db_logger, verify_admin_access
from serving.servers.routers import admin as admin_router
from serving.servers.routers.anthropic_messages import _schedule_log_store_task
from serving.storage.log_schema import ensure_api_logs_schema
from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres

if TYPE_CHECKING:
    import asyncpg

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

_PROVIDER = "sglang"
_MODEL = "deepseek-v4.1-flash"
_BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class _Pool:
    """Minimal db_logger stand-in: the route only reads ``.pool``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool


@pytest_asyncio.fixture
async def pool():
    """A pool scoped to a private schema holding a fresh ``api_logs``.

    Schema-per-worker rather than DELETE on the shared ``api_logs``: a run
    that dies before teardown (timeout, SIGKILL, Ctrl-C) leaves its rows
    behind and would poison the next run's exact-count assertions. A crashed
    run leaves a schema that the next run of the same worker drops and
    recreates, so counts stay deterministic. The connection itself comes from
    the tests' Postgres entrypoint, which prepares the worker database and
    refuses a non-test database before any DDL.
    """
    worker = os.getenv("PYTEST_XDIST_WORKER", "master")
    schema = f"cache3s_{worker}"

    admin_conn = await postgres.connect()
    try:
        await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin_conn.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin_conn.close()

    new_pool = await postgres.create_pool(
        min_size=1, max_size=3, server_settings={"search_path": schema}
    )
    async with new_pool.acquire() as conn:
        await ensure_api_logs_schema(conn)

    # Unique per fixture instance so assertions about exact counts hold per
    # test even if a future test in this module runs concurrently.
    prefix = f"cache3s-{uuid.uuid4().hex[:12]}"
    try:
        yield new_pool, prefix
    finally:
        await new_pool.execute("DELETE FROM api_logs WHERE request_id LIKE $1", f"{prefix}%")
        await new_pool.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await new_pool.close()


async def _seed(pool: asyncpg.Pool, prefix: str, cache_read_tokens: list[int | None]) -> None:
    """Insert one successful api_logs row per entry, all with 1000 prompt tokens."""
    rows = [
        (f"{prefix}-{i}", _MODEL, _PROVIDER, _BASE + timedelta(minutes=i), 1000, cached)
        for i, cached in enumerate(cache_read_tokens)
    ]
    await pool.executemany(
        "INSERT INTO api_logs (request_id, model_id, provider, timestamp,"
        " prompt_tokens, cache_read_tokens, status_code)"
        " VALUES ($1, $2, $3, $4, $5, $6, 200)",
        rows,
    )


async def _observability(pool: asyncpg.Pool) -> dict:
    app = FastAPI()
    app.state.services = AppServices(router=None, db_logger=_Pool(pool))  # type: ignore[arg-type]
    app.include_router(admin_router.router)
    app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
    app.dependency_overrides[get_db_logger] = lambda: _Pool(pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/admin/api/provider-observability",
            params={
                "provider": _PROVIDER,
                "model_id": _MODEL,
                "from": (_BASE - timedelta(hours=1)).isoformat(),
                "to": (_BASE + timedelta(hours=1)).isoformat(),
            },
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _totals(pool: asyncpg.Pool) -> dict:
    return (await _observability(pool))["totals"]


async def _ttft_scatter(pool: asyncpg.Pool) -> dict:
    app = FastAPI()
    app.state.services = AppServices(router=None, db_logger=_Pool(pool))  # type: ignore[arg-type]
    app.include_router(admin_router.router)
    app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
    app.dependency_overrides[get_db_logger] = lambda: _Pool(pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/ttft-scatter")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_null_zero_and_hit_are_three_states(pool):
    """Only reported values are eligible; NULL is neither a hit nor a miss."""
    pool, prefix = pool
    await _seed(pool, prefix, [None, 0, 900])
    response = await _observability(pool)
    totals = response["totals"]

    assert totals["request_count"] == 3
    assert totals["cache_eligible_count"] == 2  # 0 and 900, not the NULL
    assert totals["cache_hit_count"] == 1
    assert totals["input_tokens"] == 3000
    assert totals["cache_eligible_input_tokens"] == 2000
    assert totals["cache_read_tokens"] == 900
    assert totals["cache_read_tokens"] / totals["cache_eligible_input_tokens"] == 0.45
    assert sum(bucket["cache_eligible_input_tokens"] for bucket in response["buckets"]) == 2000
    assert sum(bucket["cache_read_tokens"] for bucket in response["buckets"]) == 900


@pytest.mark.asyncio
async def test_all_unknown_is_not_a_zero_percent_rate(pool):
    """An all-unknown provider has no measurable rate -- not 0%."""
    pool, prefix = pool
    await _seed(pool, prefix, [None, None, None, None])
    totals = await _totals(pool)

    assert totals["request_count"] == 4
    assert totals["cache_eligible_count"] == 0
    assert totals["cache_hit_count"] == 0


@pytest.mark.asyncio
async def test_reported_zero_stays_eligible(pool):
    """A measured miss must stay in the denominator, or it vanishes from the rate."""
    pool, prefix = pool
    await _seed(pool, prefix, [0, 0, 0])
    totals = await _totals(pool)

    assert totals["cache_eligible_count"] == 3
    assert totals["cache_hit_count"] == 0


@pytest.mark.asyncio
async def test_anthropic_logging_persists_unknown_and_measured_zero(pool):
    """The /v1/messages usage conversion reaches distinct SQL NULL and zero values."""
    import asyncio

    pool, prefix = pool
    store = PostgresLogStore(pool, store_full_prompts=False)
    original_log_request = store.log_request
    inserted = asyncio.Event()
    insert_count = 0

    async def log_request_and_signal(**kwargs):
        nonlocal insert_count
        await original_log_request(**kwargs)
        insert_count += 1
        if insert_count == 2:
            inserted.set()

    store.log_request = log_request_and_signal
    common = {
        "log_store": store,
        "model_id": _MODEL,
        "provider": _PROVIDER,
        "latency_ms": 10,
        "status_code": 200,
        "pricing": {},
        "metadata": {},
        "params": {},
        "prompt": [],
    }
    _schedule_log_store_task(
        request_id=f"{prefix}-anthropic-unknown",
        usage={"input_tokens": 1000, "output_tokens": 10},
        **common,
    )
    _schedule_log_store_task(
        request_id=f"{prefix}-anthropic-miss",
        usage={
            "input_tokens": 1000,
            "output_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        **common,
    )
    await asyncio.wait_for(inserted.wait(), timeout=3)
    await original_log_request(
        request_id=f"{prefix}-invalid-cache",
        model_id=_MODEL,
        provider=_PROVIDER,
        prompt=[],
        response=None,
        usage={"cache_read_tokens": True},
        latency_ms=10,
        status_code=200,
        pricing={},
    )

    rows = await pool.fetch(
        "SELECT request_id, cache_read_tokens, cache_write_tokens FROM api_logs"
        " WHERE request_id LIKE $1 ORDER BY request_id",
        f"{prefix}-%",
    )
    assert len(rows) == 3
    by_id = {row["request_id"]: row for row in rows}
    unknown = by_id[f"{prefix}-anthropic-unknown"]
    measured_miss = by_id[f"{prefix}-anthropic-miss"]
    assert unknown["cache_read_tokens"] is None
    assert unknown["cache_write_tokens"] is None
    assert measured_miss["cache_read_tokens"] == 0
    assert measured_miss["cache_write_tokens"] == 0
    assert by_id[f"{prefix}-invalid-cache"]["cache_read_tokens"] is None


@pytest.mark.asyncio
async def test_ttft_scatter_keeps_unknown_cache_rows_distinct(pool):
    """Unknown cache rows do not appear in either measured cache class."""
    pool, prefix = pool
    timestamp = datetime.now(UTC)
    await pool.executemany(
        "INSERT INTO api_logs (request_id, model_id, provider, timestamp, prompt_tokens,"
        " cache_read_tokens, ttft_ms, stream, status_code)"
        " VALUES ($1, $2, $3, $4, 1000, $5, $6, TRUE, 200)",
        [
            (f"{prefix}-ttft-unknown", _MODEL, _PROVIDER, timestamp, None, 100),
            (f"{prefix}-ttft-miss", _MODEL, _PROVIDER, timestamp, 0, 101),
            (f"{prefix}-ttft-hit", _MODEL, _PROVIDER, timestamp, 900, 102),
        ],
    )

    response = await _ttft_scatter(pool)
    model = next(row for row in response["models"] if row["model_id"] == _MODEL)
    points = model["points"]
    assert len(points) == 3
    assert {point["cache_hit"] for point in points} == {None, False, True}
