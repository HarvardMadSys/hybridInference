"""PostgreSQL integration tests for the admin request views' ``source`` filter.

``tests/servers/test_admin_recent_requests_source.py`` pins the query text
against a mocked connection; these run the real queries over seeded
``api_logs`` rows: Recent Requests lists the requests callers made, the Batches
tab the ones the batch worker ran (``batch_job_id`` set), and "Clear errors"
deletes only the errors of the source it is given.

Connection: the PostgreSQL test database from ``tests/fixtures/postgres.py``.
Skipped when it is not reachable (a failure under ``TEST_DB_REQUIRED``).
"""

from __future__ import annotations

import hashlib
import itertools
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from migrations.baseline.api_logs import ensure_api_logs_schema
from routing.routers import FixedRouter
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin import metrics as admin_metrics
from serving.storage.postgres_log import PostgresLogStore
from tests.fixtures import postgres

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import asyncpg

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """This worker's test database (tests/fixtures/postgres.py), reachable or skipped."""
    return postgres.worker_dsn()


@pytest_asyncio.fixture
async def pool(pg_dsn: str, request: pytest.FixtureRequest) -> AsyncGenerator[asyncpg.Pool, None]:
    """A pool scoped to a private schema holding a fresh ``api_logs``.

    Schema-per-test, as the other admin request tests do: CI runs test files
    against one database concurrently, so a shared table is not ours to empty.
    """
    worker = os.getenv("PYTEST_XDIST_WORKER", "master")
    test_digest = hashlib.sha1(request.node.name.encode()).hexdigest()[:10]
    schema = f"source_{worker}_{test_digest}"

    admin_conn = await postgres.connect()
    try:
        await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin_conn.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin_conn.close()

    created = await postgres.create_pool(
        min_size=1, max_size=3, server_settings={"search_path": schema}
    )
    assert created is not None
    async with created.acquire() as conn:
        await ensure_api_logs_schema(conn)
        # The list joins users for each row's name and email.
        await conn.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE, user_name TEXT, "
            "login_name TEXT)"
        )
        await conn.execute(
            "INSERT INTO users (id, email, user_name) VALUES ('user-1', 'ada@example.com', 'Ada')"
        )

    admin_metrics._RECENT_REQUESTS_COUNT_CACHE.clear()
    try:
        yield created
    finally:
        admin_metrics._RECENT_REQUESTS_COUNT_CACHE.clear()
        await created.close()
        admin_conn = await postgres.connect()
        try:
            await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin_conn.close()


@pytest_asyncio.fixture
async def client(pool: asyncpg.Pool) -> AsyncGenerator[AsyncClient, None]:
    app = FastAPI(title="Admin request source filter (PostgreSQL)")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=FixedRouter(), db_logger=SimpleNamespace(pool=pool)
    )
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


_ids = itertools.count(1)


def _row(*, batch: str | None = None, status: int = 200, error: str | None = None) -> Any:
    return (f"req-{next(_ids)}", batch, status, error)


async def _seed(pool: asyncpg.Pool, rows: list[Any]) -> list[str]:
    now = datetime.now(UTC)
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO api_logs (
                request_id, model_id, provider, status_code, error, user_id,
                batch_job_id, timestamp
            ) VALUES ($1, 'glm-4.6', 'zai', $3, $4, 'user-1', $2, $5)
            """,
            [(*row, now - timedelta(minutes=5 + index)) for index, row in enumerate(rows)],
        )
    return [row[0] for row in rows]


async def _remaining(pool: asyncpg.Pool) -> set[str]:
    async with pool.acquire() as conn:
        return {r["request_id"] for r in await conn.fetch("SELECT request_id FROM api_logs")}


async def test_recent_requests_and_the_batch_list_split_the_log(client, pool):
    direct = await _seed(pool, [_row() for _ in range(3)])
    batch_a = await _seed(pool, [_row(batch="batch_a") for _ in range(4)])
    batch_b = await _seed(pool, [_row(batch="batch_b") for _ in range(2)])

    async def listed(query: str) -> tuple[set[str], dict[str, Any]]:
        resp = await client.get(f"/admin/recent-requests?{query}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return {r["request_id"] for r in body["requests"]}, body

    interactive, body = await listed("source=interactive")
    assert interactive == set(direct)
    assert body["total"] == 3
    assert all(r["batch_job_id"] is None for r in body["requests"])

    batch, body = await listed("source=batch")
    assert batch == set(batch_a) | set(batch_b)
    assert body["total"] == 6

    one, body = await listed("source=batch&batch_id=batch_b")
    assert one == set(batch_b)
    assert {r["batch_job_id"] for r in body["requests"]} == {"batch_b"}

    everything, body = await listed("")
    assert everything == set(direct) | set(batch_a) | set(batch_b)
    assert body["total"] == 9


@pytest.mark.parametrize(
    ("batch", "deleted"),
    [(False, {"direct_error"}), (True, {"batch_error"}), (None, {"direct_error", "batch_error"})],
)
async def test_clearing_errors_deletes_only_the_given_sources(pool, batch, deleted):
    ids = await _seed(
        pool,
        [
            _row(status=502, error="upstream 502"),
            _row(batch="batch_a", status=503, error="upstream 503"),
            _row(),
            _row(batch="batch_a"),
        ],
    )
    named = dict(zip(("direct_error", "batch_error", "direct_ok", "batch_ok"), ids, strict=True))
    store = PostgresLogStore(pool, fence_secret=postgres.FENCE_SECRET)

    count = await store.delete_recent_error_requests(hours=1, batch=batch)

    assert count == len(deleted)
    assert await _remaining(pool) == {
        request_id for name, request_id in named.items() if name not in deleted
    }
