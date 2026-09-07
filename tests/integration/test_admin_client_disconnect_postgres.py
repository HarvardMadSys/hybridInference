"""PostgreSQL integration tests for the client-disconnect outcome class.

The mocked coverage in ``tests/servers/test_admin_client_disconnect_outcomes.py``
asserts the SQL *text*. That catches a deleted predicate but not a wrong one —
and the predicate here is easy to get wrong in a way that reads fine:

- A disconnect is **not** "status 499". Both failure handlers log whatever
  status the upstream exception carried
  (``completions_stream._extract_exception_status_code``,
  ``routing_info._status_code_from_exception``), so a provider that answers 499
  is an ordinary failure at status 499. Classifying it as a disconnect would
  take a real failure out of the error counts and out of error triage.
- The exclusion has to be ``IS NOT TRUE``, not ``NOT``: on a row with no status
  or no metadata the disconnect test is NULL, and ``NOT NULL`` is NULL — which
  would silently drop those rows out of the errors entirely.

These tests execute the real queries against Postgres over rows seeded to be
exactly those cases, so a regression to a bare ``status_code = 499`` fails here
even though the text assertions would still pass.

Connection: ``TEST_PG_DSN`` when set, otherwise the ``DB_HOST`` / ``DB_PORT`` /
``DB_NAME`` / ``DB_USER`` / ``DB_PASSWORD`` variables CI exports for its
``postgres`` service. Skipped when no database is reachable.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin
from serving.storage.log_schema import ensure_api_logs_schema
from tests.fixtures.auth_helpers import assert_test_db_name

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

# The gateway's own abandoned-stream row: 499 plus the terminal state
# ``_finalize_cancelled`` records, and the error text it writes.
_GATEWAY_DISCONNECT = "req-disconnect"
# The case this file exists for: an OpenAI-compatible upstream answered 499, so
# the status matches but nothing marked it a local cancellation. A real failure.
_UPSTREAM_499 = "req-upstream-499"
# No status came back at all. A failure, and the row that a plain ``NOT`` would
# drop out of ``errors_excluding_disconnects``.
_NULL_STATUS = "req-null-status"
_SERVER_ERROR = "req-500"
_SUCCESS = "req-ok"


def _dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if dsn:
        return dsn
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "hybridinference_test_db")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "postgres")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """A reachable test-database DSN, or skip."""
    import asyncio

    dsn = _dsn()

    async def _probe() -> str | None:
        try:
            conn = await asyncpg.connect(dsn, timeout=2)
        except Exception as exc:
            return str(exc)
        try:
            assert_test_db_name(await conn.fetchval("SELECT current_database()"), "disconnects")
        finally:
            await conn.close()
        return None

    loop = asyncio.new_event_loop()
    try:
        failure = loop.run_until_complete(_probe())
    finally:
        loop.close()
    if failure is not None:
        pytest.skip(f"PostgreSQL test database not available: {failure}")
    return dsn


@pytest_asyncio.fixture
async def db_logger(pg_dsn: str, request: pytest.FixtureRequest) -> AsyncGenerator[Any, None]:
    """A pool scoped to a private schema holding a fresh ``api_logs``.

    Schema-per-test rather than TRUNCATE: CI runs several files against one
    database concurrently, so truncating the shared ``public.api_logs`` would
    delete rows out from under whatever else is running.
    """
    worker = os.getenv("PYTEST_XDIST_WORKER", "master")
    test_digest = hashlib.sha1(request.node.name.encode()).hexdigest()[:10]
    schema = f"disconnect_{worker}_{test_digest}"

    admin_conn = await asyncpg.connect(pg_dsn)
    try:
        await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin_conn.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin_conn.close()

    pool = await asyncpg.create_pool(
        pg_dsn, min_size=1, max_size=3, server_settings={"search_path": schema}
    )
    assert pool is not None
    async with pool.acquire() as conn:
        await ensure_api_logs_schema(conn)
        # The list endpoint joins users; only the columns it reads are needed.
        await conn.execute(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                user_name TEXT
            )
            """
        )
        await conn.execute(
            "INSERT INTO users (id, email, user_name) VALUES ('user-1', 'ada@example.com', 'Ada')"
        )
        await _seed(conn)

    try:
        yield SimpleNamespace(pool=pool)
    finally:
        await pool.close()
        admin_conn = await asyncpg.connect(pg_dsn)
        try:
            await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin_conn.close()


async def _seed(conn: asyncpg.Connection) -> None:
    """Seed one row per outcome the predicates have to tell apart.

    Timestamps sit a few minutes back so every row lands inside the metrics
    endpoint's 1-hour window, whose upper bound is ``date_trunc('minute', NOW())``.
    """
    at = datetime.now(timezone.utc) - timedelta(minutes=5)
    rows: list[tuple[str, int | None, str | None, str]] = [
        (
            _GATEWAY_DISCONNECT,
            499,
            "Client disconnected before the stream completed",
            json.dumps({"terminal_state": "client_disconnect"}),
        ),
        # Same status, same non-null error, no cancellation marker.
        (_UPSTREAM_499, 499, "upstream returned 499", "{}"),
        (_NULL_STATUS, None, "no response from upstream", "{}"),
        (_SERVER_ERROR, 500, "boom", "{}"),
        (_SUCCESS, 200, None, "{}"),
    ]
    await conn.executemany(
        """
        INSERT INTO api_logs (
            request_id, model_id, provider, stream, status_code, error,
            latency_ms, prompt_tokens, completion_tokens, user_id,
            metadata, timestamp
        ) VALUES ($1,'glm-4.6','zai',TRUE,$2,$3,1200,100,50,'user-1',$4::jsonb,$5)
        """,
        [(rid, status, error, metadata, at) for rid, status, error, metadata in rows],
    )


@pytest_asyncio.fixture
async def client(db_logger: Any) -> AsyncGenerator[AsyncClient, None]:
    app = FastAPI(title="Admin Client Disconnect Postgres Test")
    app.state.services = AppServices(router=RouteExecutor(), db_logger=db_logger)  # type: ignore[attr-defined]
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _ids(client: AsyncClient, outcome: str) -> set[str]:
    resp = await client.get(f"/admin/recent-requests?outcome={outcome}&days=1")
    assert resp.status_code == 200, resp.text
    return {row["request_id"] for row in resp.json()["requests"]}


@pytest.mark.asyncio
async def test_only_the_gateway_marked_row_is_a_client_disconnect(client: AsyncClient):
    """An upstream's own 499 is not a client disconnect and must not be listed."""
    assert await _ids(client, "client_disconnect") == {_GATEWAY_DISCONNECT}


@pytest.mark.asyncio
async def test_upstream_499_survives_in_errors_excluding_disconnects(client: AsyncClient):
    """Holding back the disconnects must not take a real failure with them.

    This is the regression that a bare ``status_code = 499`` predicate causes:
    the upstream failure disappears from the list an admin triages errors in.
    The NULL-status row pins the ``IS NOT TRUE`` guard — under a plain ``NOT``
    it would be dropped here too.
    """
    assert await _ids(client, "errors_excluding_disconnects") == {
        _UPSTREAM_499,
        _NULL_STATUS,
        _SERVER_ERROR,
    }


@pytest.mark.asyncio
async def test_errors_still_covers_every_failure_including_disconnects(client: AsyncClient):
    """``outcome=errors`` keeps the meaning ``errors_only`` always had."""
    every_failure = {_GATEWAY_DISCONNECT, _UPSTREAM_499, _NULL_STATUS, _SERVER_ERROR}
    assert await _ids(client, "errors") == every_failure
    # The legacy flag is the same predicate, so it selects the same rows.
    resp = await client.get("/admin/recent-requests?errors_only=true&days=1")
    assert resp.status_code == 200, resp.text
    assert {row["request_id"] for row in resp.json()["requests"]} == every_failure


@pytest.mark.asyncio
async def test_all_outcome_lists_every_row(client: AsyncClient):
    assert await _ids(client, "all") == {
        _GATEWAY_DISCONNECT,
        _UPSTREAM_499,
        _NULL_STATUS,
        _SERVER_ERROR,
        _SUCCESS,
    }


@pytest.mark.asyncio
async def test_list_exposes_the_terminal_state_that_classified_the_row(client: AsyncClient):
    """The console badges on this, so it has to reach the payload."""
    resp = await client.get("/admin/recent-requests?days=1")
    assert resp.status_code == 200, resp.text
    by_id = {row["request_id"]: row for row in resp.json()["requests"]}
    assert by_id[_GATEWAY_DISCONNECT]["terminal_state"] == "client_disconnect"
    # Same status code, no marker — the console must not badge it as a hang-up.
    assert by_id[_UPSTREAM_499]["terminal_state"] is None


@pytest.mark.asyncio
async def test_metrics_counts_only_the_marked_row_as_a_disconnect(client: AsyncClient):
    """The hour card's split, over the real query.

    The upstream 499 has to stay in ``error_count``: it is the failure the split
    is at risk of hiding.
    """
    resp = await client.get("/admin/request-metrics")
    assert resp.status_code == 200, resp.text
    hour = next(w for w in resp.json()["windows"] if w["key"] == "1h")

    assert hour["total_requests"] == 5
    assert hour["success_requests"] == 1
    assert hour["client_disconnect_requests"] == 1
    # upstream 499 + NULL status + 500.
    assert hour["error_requests"] == 3
    # Together they account for the window, with nothing counted twice.
    assert (
        hour["success_requests"] + hour["error_requests"] + hour["client_disconnect_requests"]
        == hour["total_requests"]
    )
