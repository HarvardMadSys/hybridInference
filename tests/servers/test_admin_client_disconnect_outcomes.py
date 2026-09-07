"""Client disconnects are their own outcome class in the Recent Requests views.

A caller that hangs up mid-stream is logged by
``completions_stream._finalize_cancelled`` as status 499 with a non-null
``error``, so the historical "errors only" predicate swept it up alongside real
failures — an hour of abandoned streams read as an hour of outage. These tests
pin the split:

- ``/admin/request-metrics`` counts 499s in ``client_disconnect_count`` and
  keeps them out of ``error_count``, so the two can be read side by side.
- ``/admin/recent-requests`` and the JSONL export accept an ``outcome`` filter
  that can isolate the disconnects or hold them out of the error list, while the
  older ``errors_only`` boolean keeps its exact meaning.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin._common import CLIENT_DISCONNECT_STATUS_CODE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# One bucket with a mix of every outcome. The endpoint copies these counts
# through, so the assertions below are on which SQL column each lands in — the
# predicates that produce them are pinned separately, against the query text.
_BUCKET = {
    "request_count": 10,
    "success_count": 6,
    "error_count": 1,
    "client_disconnect_count": 3,
    "latency_count": 6,
    "latency_sum_ms": 1200.0,
    "avg_latency_ms": 200.0,
}


def _make_db_logger_with_capture(
    fetch_rows: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, list[Any]]]:
    """Mock db_logger whose conn records every fetch/fetchrow call.

    ``fetch_rows`` is returned from every ``fetch`` — the metrics endpoint reads
    its rows by key, so one canned bucket serves every lookback window.
    """
    calls: dict[str, list[Any]] = {"fetchrow": [], "fetch": []}

    async def _fetchrow(query: str, *args: Any) -> Any:
        calls["fetchrow"].append((query, args))
        if "COUNT(*)" in query:
            return {"total": 0}
        return None

    async def _fetch(query: str, *args: Any) -> list[Any]:
        calls["fetch"].append((query, args))
        return list(fetch_rows or [])

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetch = AsyncMock(side_effect=_fetch)

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)

    logger = MagicMock()
    logger.pool = pool
    logger.log_admin_action = AsyncMock()

    return logger, calls


def _build_app(logger: Any) -> FastAPI:
    app = FastAPI(title="Admin Client Disconnect Test")
    app.state.services = AppServices(router=RouteExecutor(), db_logger=logger)  # type: ignore[attr-defined]
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)
    return app


@pytest.fixture
async def admin_client_capture() -> AsyncGenerator[tuple[AsyncClient, dict[str, list[Any]]], None]:
    logger, calls = _make_db_logger_with_capture()
    transport = ASGITransport(app=_build_app(logger))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, calls


# ---------------------------------------------------------------------------
# /admin/request-metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_metrics_counts_disconnects_apart_from_errors():
    """499 lands in client_disconnect_count and is excluded from error_count."""
    bucket_start = datetime.now(timezone.utc) - timedelta(minutes=5)
    logger, calls = _make_db_logger_with_capture([{"bucket_start": bucket_start, **_BUCKET}])
    transport = ASGITransport(app=_build_app(logger))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/request-metrics")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    hour = next(window for window in body["windows"] if window["key"] == "1h")
    assert hour["client_disconnect_requests"] == 3
    assert hour["error_requests"] == 1
    assert hour["buckets"][0]["client_disconnect_count"] == 3
    assert hour["buckets"][0]["error_count"] == 1

    # The status is bound, not interpolated, and the error filter excludes it.
    query, args = calls["fetch"][0]
    assert args[2] == CLIENT_DISCONNECT_STATUS_CODE
    assert "AND status_code IS DISTINCT FROM $3::int" in query
    assert "WHERE status_code = $3::int\n" in query


# ---------------------------------------------------------------------------
# /admin/recent-requests — outcome filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_sql"),
    [
        ("client_disconnect", "l.status_code = 499"),
        (
            "errors_excluding_disconnects",
            "AND l.status_code IS DISTINCT FROM 499",
        ),
        ("errors", "l.error IS NOT NULL"),
    ],
)
async def test_list_outcome_filter_reaches_both_queries(
    admin_client_capture, outcome: str, expected_sql: str
):
    """The outcome predicate scopes the page *and* the total shown beside it."""
    client, calls = admin_client_capture
    resp = await client.get(f"/admin/recent-requests?outcome={outcome}")
    assert resp.status_code == 200, resp.text

    count_query, _ = calls["fetchrow"][0]
    select_query, _ = calls["fetch"][0]
    assert expected_sql in count_query
    assert expected_sql in select_query


@pytest.mark.asyncio
async def test_list_outcome_all_applies_no_predicate(admin_client_capture):
    client, calls = admin_client_capture
    resp = await client.get("/admin/recent-requests?outcome=all")
    assert resp.status_code == 200, resp.text
    select_query, _ = calls["fetch"][0]
    assert "l.status_code = 499" not in select_query
    assert "l.error IS NOT NULL" not in select_query


@pytest.mark.asyncio
async def test_list_errors_only_still_includes_disconnects(admin_client_capture):
    """``errors_only`` keeps its old meaning — the whole non-success set.

    Bookmarked admin URLs and any external caller still get exactly the rows
    they got before the outcome vocabulary existed; narrowing that predicate
    would silently change what an unchanged request returns.
    """
    client, calls = admin_client_capture
    resp = await client.get("/admin/recent-requests?errors_only=true")
    assert resp.status_code == 200, resp.text
    select_query, _ = calls["fetch"][0]
    assert "l.error IS NOT NULL" in select_query
    assert "IS DISTINCT FROM 499" not in select_query


@pytest.mark.asyncio
async def test_list_outcome_wins_over_errors_only(admin_client_capture):
    client, calls = admin_client_capture
    resp = await client.get("/admin/recent-requests?errors_only=true&outcome=client_disconnect")
    assert resp.status_code == 200, resp.text
    select_query, _ = calls["fetch"][0]
    assert "l.status_code = 499" in select_query
    assert "l.error IS NOT NULL" not in select_query


@pytest.mark.asyncio
async def test_list_rejects_unknown_outcome(admin_client_capture):
    """A mistyped outcome is a 422, never a silently unfiltered page.

    Answering "show me the client disconnects" with the whole stream is
    indistinguishable, to the admin reading it, from a window that had none.
    """
    client, calls = admin_client_capture
    resp = await client.get("/admin/recent-requests?outcome=client_disconnects")
    assert resp.status_code == 422, resp.text
    assert "client_disconnect" in resp.json()["detail"]
    assert not calls["fetch"], "must not query the database on a rejected filter"


@pytest.mark.asyncio
async def test_outcome_composes_with_paging_placeholders(admin_client_capture):
    """The predicate is constant, so LIMIT/OFFSET keep their bind positions."""
    client, calls = admin_client_capture
    resp = await client.get(
        "/admin/recent-requests?outcome=client_disconnect&model_id=glm&limit=25&offset=50"
    )
    assert resp.status_code == 200, resp.text
    select_query, select_args = calls["fetch"][0]
    # days, model_id, then the paging pair — the outcome adds no parameter.
    assert select_args == (7, "glm", 25, 50)
    assert "LIMIT $3 OFFSET $4" in select_query
