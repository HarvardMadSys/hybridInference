"""PostgreSQL integration tests for the admin offloaded-requests summary.

``tests/servers/test_admin_recent_requests_offloads.py`` pins the query text
against a mocked connection; these run the real query over seeded ``api_logs``
rows and check the numbers: which rows count as offloaded, how they split by
route and reason, what a model's total is, and what counts as a failure.

Connection: ``TEST_PG_DSN`` when set, otherwise the ``DB_HOST`` / ``DB_PORT`` /
``DB_NAME`` / ``DB_USER`` / ``DB_PASSWORD`` variables CI exports for its
``postgres`` service, as ``test_admin_request_perf_breakdown_postgres.py`` does.
Skipped when no database is reachable.
"""

from __future__ import annotations

import hashlib
import itertools
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
from serving.servers.routers.admin import metrics as admin_metrics
from serving.servers.routers.admin.metrics import _load_request_offloads
from serving.storage.log_schema import ensure_api_logs_schema
from tests.fixtures.auth_helpers import assert_test_db_name

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

MODEL = "glm-4.6"
ENGINE = "glm-4.6:local-12003"
OFFLOAD = "glm-4.6:reserved-api"


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
            assert_test_db_name(await conn.fetchval("SELECT current_database()"), "offloads")
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

    Schema-per-test for the reason the perf-breakdown tests give: CI runs test
    files against one database concurrently, so a shared table is not ours to
    truncate.
    """
    worker = os.getenv("PYTEST_XDIST_WORKER", "master")
    test_digest = hashlib.sha1(request.node.name.encode()).hexdigest()[:10]
    schema = f"offloads_{worker}_{test_digest}"

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
        # The user filter joins users; only the columns it reads are needed.
        await conn.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE, user_name TEXT, "
            "login_name TEXT)"
        )
        await conn.execute(
            "INSERT INTO users (id, email, user_name) VALUES "
            "('user-1', 'ada@example.com', 'Ada'), ('user-2', 'bob@example.com', 'Bob')"
        )

    admin_metrics._OFFLOAD_SUMMARY_CACHE.clear()
    try:
        yield SimpleNamespace(pool=pool)
    finally:
        admin_metrics._OFFLOAD_SUMMARY_CACHE.clear()
        await pool.close()
        admin_conn = await asyncpg.connect(pg_dsn)
        try:
            await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin_conn.close()


_ids = itertools.count(1)


def _row(
    *,
    offload: str | None = None,
    offload_endpoint: str | None = None,
    endpoint: str = ENGINE,
    model: str = MODEL,
    served_model: str | None = MODEL,
    status: int = 200,
    error: str | None = None,
    terminal_state: str | None = None,
    user_id: str = "user-1",
    request_type: str | None = None,
    age_hours: float = 1,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if offload is not None:
        metadata["offload"] = offload
        metadata["fallback"] = offload != "last_resort"
    if offload_endpoint is not None:
        metadata["offload_endpoint_id"] = offload_endpoint
    if terminal_state is not None:
        metadata["terminal_state"] = terminal_state
    if request_type is not None:
        metadata["request_type"] = request_type
    return {
        "request_id": f"req-{next(_ids)}",
        "model_id": model,
        "provider": endpoint.split(":", 1)[-1],
        "served_model_id": served_model,
        "served_endpoint_id": endpoint,
        "status_code": status,
        "error": error,
        "user_id": user_id,
        "metadata": metadata,
        "age_hours": age_hours,
    }


async def _seed(pool: asyncpg.Pool, rows: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO api_logs (
                request_id, model_id, provider, served_model_id, served_endpoint_id,
                stream, status_code, error, user_id, metadata, timestamp
            ) VALUES ($1, $2, $3, $4, $5, TRUE, $6, $7, $8, $9::jsonb, $10)
            """,
            [
                (
                    r["request_id"],
                    r["model_id"],
                    r["provider"],
                    r["served_model_id"],
                    r["served_endpoint_id"],
                    r["status_code"],
                    r["error"],
                    r["user_id"],
                    json.dumps(r["metadata"]),
                    now - timedelta(hours=r["age_hours"]),
                )
                for r in rows
            ],
        )


async def _summary(db_logger, **filters: Any):
    return await _load_request_offloads(
        db_logger,
        days=filters.pop("days", 1),
        user_id=filters.pop("user_id", None),
        session_id=filters.pop("session_id", None),
        model_id=filters.pop("model_id", None),
        request_type=filters.pop("request_type", None),
    )


async def test_offloads_split_by_reason_against_the_models_whole_traffic(db_logger):
    await _seed(
        db_logger.pool,
        [_row() for _ in range(6)]
        + [_row(offload="queue_wait", endpoint=OFFLOAD) for _ in range(3)]
        + [_row(offload="engine_wait", endpoint=OFFLOAD) for _ in range(2)]
        + [_row(offload="last_resort", endpoint=OFFLOAD)]
        # Another model that never offloaded: no group, and none of its traffic
        # counts toward glm-4.6's total.
        + [
            _row(model="qwen3", served_model="qwen3", endpoint="qwen3:local-12004")
            for _ in range(4)
        ],
    )

    summary = await _summary(db_logger)

    assert summary.total_offloaded == 6
    assert summary.truncated is False
    (group,) = summary.groups
    assert (group.model_id, group.endpoint_id) == (MODEL, OFFLOAD)
    assert group.reasons == {"queue_wait": 3, "engine_wait": 2, "last_resort": 1}
    assert group.request_count == 6
    assert group.model_request_count == 12
    assert group.failed_count == 0


async def test_a_failure_on_the_offload_route_counts_but_a_disconnect_does_not(db_logger):
    await _seed(
        db_logger.pool,
        [
            _row(offload="engine_wait", endpoint=OFFLOAD),
            _row(offload="engine_wait", endpoint=OFFLOAD, status=502, error="upstream 502"),
            # The caller hung up mid-stream: not a failure of the offload route.
            _row(
                offload="queue_wait",
                endpoint=OFFLOAD,
                status=499,
                error="client disconnected",
                terminal_state="client_disconnect",
            ),
            # A 499 the upstream itself answered, without the gateway's marker.
            _row(offload="queue_wait", endpoint=OFFLOAD, status=499, error="upstream 499"),
        ],
    )

    (group,) = (await _summary(db_logger)).groups

    assert group.request_count == 4
    assert group.failed_count == 2


async def test_a_failed_offload_counts_under_the_route_it_was_sent_to(db_logger):
    """A failure's row is the route whose error was reported, usually the primary."""
    await _seed(
        db_logger.pool,
        [
            _row(offload="engine_wait", endpoint=OFFLOAD),
            # The offload route failed too, and the primary's queue wait was the
            # error reported: the row is the engine's, the marker names the route.
            _row(
                offload="queue_wait",
                offload_endpoint=OFFLOAD,
                endpoint=ENGINE,
                status=503,
                error="queue wait expired",
            ),
            _row(),
        ],
    )

    summary = await _summary(db_logger)

    assert summary.total_offloaded == 2
    (group,) = summary.groups
    assert (group.model_id, group.endpoint_id) == (MODEL, OFFLOAD)
    assert group.reasons == {"engine_wait": 1, "queue_wait": 1}
    assert group.failed_count == 1
    assert group.model_request_count == 3


async def test_an_offload_a_later_route_served_counts_under_the_route_it_was_sent_to(db_logger):
    """The offload route failed and another route served: offloaded, not failed."""
    sibling = f"{MODEL}:sibling-api"
    await _seed(
        db_logger.pool,
        [
            _row(offload="queue_wait", endpoint=OFFLOAD),
            _row(offload="queue_wait", offload_endpoint=OFFLOAD, endpoint=sibling),
            _row(endpoint=sibling),
        ],
    )

    summary = await _summary(db_logger)

    (group,) = summary.groups
    assert (group.model_id, group.endpoint_id) == (MODEL, OFFLOAD)
    assert group.reasons == {"queue_wait": 2}
    assert group.failed_count == 0
    assert group.model_request_count == 3


async def test_only_the_window_counts(db_logger):
    await _seed(
        db_logger.pool,
        [
            _row(offload="queue_wait", endpoint=OFFLOAD, age_hours=2),
            _row(age_hours=3),
            _row(offload="queue_wait", endpoint=OFFLOAD, age_hours=30),
            _row(age_hours=40),
        ],
    )

    (day,) = (await _summary(db_logger)).groups
    (week,) = (await _summary(db_logger, days=7)).groups

    assert (day.request_count, day.model_request_count) == (1, 2)
    assert (week.request_count, week.model_request_count) == (2, 4)


async def test_a_model_whose_offload_route_moved_has_a_group_per_route(db_logger):
    await _seed(
        db_logger.pool,
        [_row() for _ in range(5)]
        + [_row(offload="queue_wait", endpoint="glm-4.6:old-reserved-api")]
        + [_row(offload="queue_wait", endpoint=OFFLOAD) for _ in range(3)],
    )

    summary = await _summary(db_logger)

    assert [(g.endpoint_id, g.request_count) for g in summary.groups] == [
        (OFFLOAD, 3),
        ("glm-4.6:old-reserved-api", 1),
    ]
    # Both are read against the same model total.
    assert {g.model_request_count for g in summary.groups} == {9}
    assert summary.total_offloaded == 4


async def test_rows_logged_before_the_served_columns_fall_back_to_model_and_provider(db_logger):
    legacy = _row(offload="queue_wait", endpoint=OFFLOAD, served_model=None)
    legacy["served_endpoint_id"] = None
    await _seed(db_logger.pool, [legacy])

    (group,) = (await _summary(db_logger)).groups

    assert (group.model_id, group.endpoint_id) == (MODEL, "reserved-api")


async def test_the_list_views_filters_narrow_the_same_rows(db_logger):
    await _seed(
        db_logger.pool,
        [
            _row(offload="queue_wait", endpoint=OFFLOAD, user_id="user-1"),
            _row(offload="queue_wait", endpoint=OFFLOAD, user_id="user-2"),
            _row(user_id="user-2"),
            _row(
                offload="queue_wait", endpoint=OFFLOAD, user_id="user-2", request_type="embedding"
            ),
        ],
    )

    (bob,) = (await _summary(db_logger, user_id="bob")).groups
    (chat,) = (await _summary(db_logger, user_id="bob", request_type="chat")).groups
    unmatched = await _summary(db_logger, model_id="qwen")

    assert (bob.request_count, bob.model_request_count) == (2, 3)
    assert (chat.request_count, chat.model_request_count) == (1, 2)
    assert unmatched.groups == []
    assert unmatched.total_offloaded == 0


async def test_the_endpoint_serves_the_summary(db_logger):
    await _seed(
        db_logger.pool,
        [_row(), _row(offload="engine_wait", endpoint=OFFLOAD, status=502, error="boom")],
    )
    app = FastAPI(title="Admin Offloads Postgres Test")
    app.state.services = AppServices(router=RouteExecutor(), db_logger=db_logger)  # type: ignore[attr-defined]
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/recent-requests/offloads")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["days"] == 1
    assert body["total_offloaded"] == 1
    assert body["groups"] == [
        {
            "model_id": MODEL,
            "endpoint_id": OFFLOAD,
            "request_count": 1,
            "failed_count": 1,
            "reasons": {"engine_wait": 1},
            "model_request_count": 2,
        }
    ]
