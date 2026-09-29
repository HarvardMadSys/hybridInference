"""Tests for ``GET /admin/recent-requests/offloads`` against a mocked connection.

These pin the endpoint's wiring -- the window, the filters it shares with the
list view, how grouped rows fold into one group per served route, the cap and
the cache. ``tests/integration/test_admin_request_offloads_postgres.py`` runs
the real query and checks the numbers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin import metrics as admin_metrics

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

URL = "/admin/recent-requests/offloads"


def _db_logger(rows: list[dict[str, Any]] | None = None) -> tuple[Any, list[tuple[str, tuple]]]:
    """A db_logger whose connection records every ``fetch`` and returns ``rows``."""
    calls: list[tuple[str, tuple]] = []

    async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        calls.append((query, args))
        return list(rows or [])

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=_fetch)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    logger = MagicMock()
    logger.pool = pool
    return logger, calls


def _row(
    reason: str,
    count: int,
    *,
    model: str = "glm-4.6",
    endpoint: str = "glm-4.6:reserved-api",
    failed: int = 0,
    model_total: int = 100,
) -> dict[str, Any]:
    return {
        "served_model": model,
        "route": endpoint,
        "reason": reason,
        "request_count": count,
        "failed_count": failed,
        "model_request_count": model_total,
    }


@pytest.fixture(autouse=True)
def _isolate_cache():
    admin_metrics._OFFLOAD_SUMMARY_CACHE.clear()
    yield
    admin_metrics._OFFLOAD_SUMMARY_CACHE.clear()


def _app(logger: Any, *, admin_override: bool = True) -> FastAPI:
    app = FastAPI(title="Admin Recent Requests Offloads Test")
    app.state.services = AppServices(router=RouteExecutor(), db_logger=logger)  # type: ignore[attr-defined]
    if admin_override:
        app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)
    return app


async def _client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def offloads_client(request: pytest.FixtureRequest):
    """A client over a mocked pool; parametrize indirectly with the rows to return."""
    logger, calls = _db_logger(getattr(request, "param", None))
    async for client in _client(_app(logger)):
        yield client, calls


async def test_the_window_is_the_past_day_by_default(offloads_client):
    client, calls = offloads_client

    resp = await client.get(URL)

    assert resp.status_code == 200, resp.text
    ((query, args),) = calls
    assert "l.timestamp >= NOW() - make_interval(days => $1::int)" in query
    assert args[0] == 1
    assert resp.json()["days"] == 1
    # No users join when no user filter is active (api_logs is high-volume).
    assert "LEFT JOIN users u" not in query


@pytest.mark.parametrize(("query_string", "expected_days"), [("?days=0", 1), ("?days=365", 90)])
async def test_the_window_is_clamped(offloads_client, query_string, expected_days):
    client, calls = offloads_client

    resp = await client.get(URL + query_string)

    assert resp.status_code == 200, resp.text
    assert calls[0][1][0] == expected_days


async def test_offloaded_rows_are_read_from_the_logged_marker(offloads_client):
    """One scan: every row counts toward its model, the marked ones are offloads."""
    client, calls = offloads_client

    assert (await client.get(URL)).status_code == 200

    query, _args = calls[0]
    assert "NULLIF(l.metadata->>'offload', '') AS reason" in query
    assert "COALESCE(NULLIF(l.served_model_id, ''), l.model_id) AS served_model" in query
    # A failed offload's row is the route whose error was reported; the offload
    # route it was sent to is its ``offload_endpoint_id``.
    assert (
        "COALESCE(\n"
        "                        NULLIF(l.metadata->>'offload_endpoint_id', ''),\n"
        "                        NULLIF(l.served_endpoint_id, ''),\n"
        "                        l.provider\n"
        "                    ) AS route"
    ) in query
    assert "GROUP BY served_model, route, reason" in query
    assert "SUM(COUNT(*)) OVER (PARTITION BY served_model) AS model_request_count" in query
    assert "WHERE reason IS NOT NULL" in query
    # Failures are the outcome filter's, client disconnects excluded, and no
    # outcome narrows the scan itself.
    failed_sql = admin_metrics.REQUEST_OUTCOME_FILTERS["errors_excluding_disconnects"]
    assert f"{failed_sql} AS failed" in query
    assert "COUNT(*) FILTER (WHERE failed) AS failed_count" in query


async def test_the_list_views_filters_apply(offloads_client):
    client, calls = offloads_client

    resp = await client.get(
        URL,
        params={
            "user_id": "ada",
            "session_id": "sess-1",
            "model_id": "glm",
            "request_type": "chat",
        },
    )

    assert resp.status_code == 200, resp.text
    query, args = calls[0]
    assert "LEFT JOIN users u ON u.id = l.user_id" in query
    assert "l.session_id = $" in query
    assert "l.model_id ILIKE" in query
    assert "(l.metadata->>'request_type') IS DISTINCT FROM 'embedding'" in query
    assert args == (1, "ada", "sess-1", "glm")


async def test_outcome_filters_are_not_accepted_as_narrowing(offloads_client):
    """Every offloaded request is counted; the failures are a column, not a filter."""
    client, calls = offloads_client

    resp = await client.get(URL, params={"outcome": "errors", "errors_only": "true"})

    assert resp.status_code == 200, resp.text
    query, _args = calls[0]
    assert "WHERE (l.error IS NOT NULL" not in query
    assert "AND (l.error IS NOT NULL OR l.status_code IS NULL" not in query


@pytest.mark.parametrize(
    "offloads_client",
    [
        [
            _row("queue_wait", 5, failed=1, model_total=40),
            _row("engine_wait", 3, model_total=40),
            _row("last_resort", 1, failed=1, model_total=40),
            _row(
                "engine_wait",
                7,
                model="qwen3-coder",
                endpoint="qwen3-coder:openrouter-api",
                model_total=70,
            ),
        ]
    ],
    indirect=True,
)
async def test_rows_fold_into_one_group_per_served_route(offloads_client):
    client, _calls = offloads_client

    body = (await client.get(URL)).json()

    assert body["total_offloaded"] == 16
    assert body["truncated"] is False
    assert body["groups"] == [
        {
            "model_id": "glm-4.6",
            "endpoint_id": "glm-4.6:reserved-api",
            "request_count": 9,
            "failed_count": 2,
            "reasons": {"queue_wait": 5, "engine_wait": 3, "last_resort": 1},
            "model_request_count": 40,
        },
        {
            "model_id": "qwen3-coder",
            "endpoint_id": "qwen3-coder:openrouter-api",
            "request_count": 7,
            "failed_count": 0,
            "reasons": {"engine_wait": 7},
            "model_request_count": 70,
        },
    ]


@pytest.mark.parametrize("offloads_client", [[]], indirect=True)
async def test_a_day_without_offloads_is_an_empty_summary(offloads_client):
    client, _calls = offloads_client

    body = (await client.get(URL)).json()

    assert body["total_offloaded"] == 0
    assert body["groups"] == []
    assert body["truncated"] is False


async def test_the_cap_keeps_the_busiest_groups_and_the_whole_total(monkeypatch):
    monkeypatch.setattr(admin_metrics, "_OFFLOAD_SUMMARY_MAX_GROUPS", 1)
    logger, _calls = _db_logger(
        [
            _row("queue_wait", 2, model="quiet", endpoint="quiet:reserved-api"),
            _row("queue_wait", 9, model="busy", endpoint="busy:reserved-api"),
        ]
    )

    async for client in _client(_app(logger)):
        body = (await client.get(URL)).json()

    assert body["truncated"] is True
    assert [group["model_id"] for group in body["groups"]] == ["busy"]
    assert body["total_offloaded"] == 11


async def test_a_repeat_request_is_served_from_the_cache(offloads_client):
    client, calls = offloads_client

    assert (await client.get(URL)).status_code == 200
    assert (await client.get(URL)).status_code == 200
    assert len(calls) == 1, "identical filters should not rescan"

    assert (await client.get(URL + "?refresh=true")).status_code == 200
    assert len(calls) == 2, "refresh must bypass the cache"

    assert (await client.get(URL + "?model_id=glm")).status_code == 200
    assert len(calls) == 3, "other filters are another cache entry"


async def test_without_a_database_it_is_a_server_error():
    logger = MagicMock()
    logger.pool = None

    async for client in _client(_app(logger)):
        resp = await client.get(URL)

    assert resp.status_code == 500


async def test_it_requires_admin_auth():
    logger, calls = _db_logger()

    async for client in _client(_app(logger, admin_override=False)):
        resp = await client.get(URL)

    assert resp.status_code in (401, 403), resp.text
    assert calls == []
