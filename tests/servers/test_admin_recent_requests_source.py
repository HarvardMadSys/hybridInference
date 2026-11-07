"""The admin request views' ``source`` and ``batch_id`` filters.

Recent Requests lists the requests callers made; the Batches tab lists the ones
the batch worker ran, which carry their batch in ``api_logs.batch_job_id``.
These pin that every view on those tabs takes the same filter -- the list and
its total, the per-route summaries, the JSONL export and "Clear errors" -- so
none of them describes a different slice of traffic than the table beside it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.servers.deps import AppServices, get_admin_audit_actor, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin import metrics, request_volume
from serving.servers.routers.admin._common import (
    REQUEST_SOURCE_FILTERS,
    request_source_is_batch,
    resolve_request_source_filter,
)
from tests.fixtures.audit import ADMIN_ACTOR

INTERACTIVE = "l.batch_job_id IS NULL"
BATCH = "l.batch_job_id IS NOT NULL"
SUMMARIES = (
    "/admin/recent-requests/performance",
    "/admin/recent-requests/offloads",
    "/admin/recent-requests/performance/trend",
)
EXPORT = "/admin/export/requests?start_time=2026-10-01T00:00:00Z"


def _row(batch_job_id: str | None = None) -> dict[str, Any]:
    """One logged request with every column the list and the export read."""
    return {
        "request_id": "req-1",
        "user_id": "user-1",
        "user_name": "Ada",
        "user_email": "ada@example.com",
        "model_id": "glm-4.6",
        "provider": "zai",
        "timestamp": datetime(2026, 10, 1, tzinfo=UTC),
        "status_code": 200,
        "latency_ms": 1200,
        "ttft_ms": None,
        "stream": False,
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "total_tokens": 30,
        "cost_usd": None,
        "error": None,
        "user_ip": None,
        "peer_ip": None,
        "ip_source": None,
        "x_forwarded_for": None,
        "user_agent": "hybridinference-batch-worker",
        "referer": None,
        "agent": None,
        "session_id": None,
        "session_id_source": None,
        "request_surface": None,
        "request_type": None,
        "credential_state": None,
        "credential_owner_id": None,
        "terminal_state": None,
        "routewise": None,
        "num_turns": None,
        "num_user_turns": None,
        "num_tool_calls": None,
        "batch_job_id": batch_job_id,
    }


class _Pool:
    """A pool whose connection records every statement and answers ``rows``.

    ``fetch`` answers the page (or a summary's groups: none, for ``rows=()``)
    and ``fetchrow`` the list's total.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.counts: list[tuple[str, tuple[Any, ...]]] = []

        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=self._fetch)
        conn.fetchrow = AsyncMock(side_effect=self._fetchrow)
        conn.execute = AsyncMock(return_value="OK")
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=conn)
        acquire.__aexit__ = AsyncMock(return_value=None)
        self.pool = MagicMock()
        self.pool.acquire = MagicMock(return_value=acquire)

    async def _fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((query, args))
        return list(self.rows)

    async def _fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.counts.append((query, args))
        return {"total": len(self.rows)}

    def statements(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [*self.fetches, *self.counts]


@pytest.fixture(autouse=True)
def _isolate_caches():
    caches = (
        metrics._RECENT_REQUESTS_COUNT_CACHE,
        metrics._PERF_BREAKDOWN_CACHE,
        metrics._PERF_TREND_CACHE,
        metrics._OFFLOAD_SUMMARY_CACHE,
    )
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


def _app(pool: _Pool, **services: Any) -> FastAPI:
    app = FastAPI(title="Admin request source filter test")
    db_logger = MagicMock(pool=pool.pool)
    db_logger.log_admin_action = AsyncMock()
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=FixedRouter(), db_logger=db_logger, **services
    )
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_admin_audit_actor] = lambda: ADMIN_ACTOR
    app.include_router(admin.router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_the_source_vocabulary():
    assert resolve_request_source_filter(None) == ""
    assert resolve_request_source_filter("") == ""
    assert resolve_request_source_filter("all") == ""
    assert resolve_request_source_filter("interactive") == INTERACTIVE
    assert resolve_request_source_filter("batch") == BATCH
    assert [request_source_is_batch(s) for s in (None, "all", "interactive", "batch")] == [
        None,
        None,
        False,
        True,
    ]
    assert set(REQUEST_SOURCE_FILTERS) == {"all", "interactive", "batch"}


@pytest.mark.parametrize(
    ("query", "present", "absent"),
    [
        ("", None, (INTERACTIVE, BATCH)),
        ("?source=all", None, (INTERACTIVE, BATCH)),
        ("?source=interactive", INTERACTIVE, (BATCH,)),
        ("?source=batch", BATCH, (INTERACTIVE,)),
    ],
)
async def test_the_list_and_its_total_take_the_source(query, present, absent):
    pool = _Pool([_row()])
    async with _client(_app(pool)) as client:
        resp = await client.get("/admin/recent-requests" + query)

    assert resp.status_code == 200, resp.text
    ((page_sql, _),) = pool.fetches
    ((count_sql, _),) = pool.counts
    for sql in (page_sql, count_sql):
        if present:
            assert present in sql
        for other in absent:
            assert other not in sql


async def test_a_batch_id_narrows_the_list_to_that_batch_and_rows_name_their_batch():
    pool = _Pool([_row("batch_abc")])
    async with _client(_app(pool)) as client:
        resp = await client.get("/admin/recent-requests?source=batch&batch_id=batch_abc")

    assert resp.status_code == 200, resp.text
    page_sql, page_args = pool.fetches[0]
    # $1 is the lookback; the batch is the only other filter.
    assert "l.batch_job_id = $2" in page_sql
    assert page_args[1] == "batch_abc"
    assert resp.json()["requests"][0]["batch_job_id"] == "batch_abc"


async def test_an_interactive_row_names_no_batch():
    pool = _Pool([_row()])
    async with _client(_app(pool)) as client:
        resp = await client.get("/admin/recent-requests?source=interactive")

    assert resp.json()["requests"][0]["batch_job_id"] is None


async def test_each_source_is_counted_apart():
    pool = _Pool([_row()])
    async with _client(_app(pool)) as client:
        for source in ("interactive", "batch", "interactive"):
            resp = await client.get(f"/admin/recent-requests?source={source}")
            assert resp.status_code == 200, resp.text

    # The third reuses the first's total; the batch total is its own.
    assert len(pool.counts) == 2


@pytest.mark.parametrize("path", SUMMARIES)
async def test_the_summaries_take_the_source_and_cache_each_apart(path):
    pool = _Pool()
    async with _client(_app(pool)) as client:
        for query in ("?source=interactive", "?source=batch", "?source=batch&batch_id=b1"):
            resp = await client.get(path + query)
            assert resp.status_code == 200, resp.text

    assert len(pool.fetches) == 3, "each filter is a summary of its own"
    (interactive_sql, _), (batch_sql, _), (one_batch_sql, one_batch_args) = pool.fetches
    assert INTERACTIVE in interactive_sql and BATCH not in interactive_sql
    assert BATCH in batch_sql and INTERACTIVE not in batch_sql
    assert "l.batch_job_id = $" in one_batch_sql
    assert "b1" in one_batch_args


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin/recent-requests?source=nope"),
        *(("GET", f"{path}?source=nope") for path in SUMMARIES),
        ("GET", f"{EXPORT}&source=nope"),
        ("POST", "/admin/recent-requests/clear-errors?source=nope"),
    ],
)
async def test_an_unknown_source_is_refused_before_anything_runs(method, path):
    pool = _Pool([_row()])
    log_store = MagicMock()
    log_store.delete_recent_error_requests = AsyncMock(return_value=0)
    async with _client(_app(pool, log_store=log_store)) as client:
        resp = await client.request(method, path)

    assert resp.status_code == 422, resp.text
    assert "source" in resp.text
    assert pool.statements() == []
    log_store.delete_recent_error_requests.assert_not_awaited()


@pytest.mark.parametrize(
    ("query", "batch"),
    [("", None), ("&source=all", None), ("&source=interactive", False), ("&source=batch", True)],
)
async def test_clearing_errors_takes_the_source(query, batch):
    log_store = MagicMock()
    log_store.delete_recent_error_requests = AsyncMock(return_value=2)
    async with _client(_app(_Pool(), log_store=log_store)) as client:
        resp = await client.post(f"/admin/recent-requests/clear-errors?hours=1{query}")

    assert resp.status_code == 200, resp.text
    log_store.delete_recent_error_requests.assert_awaited_once_with(
        hours=1, on_deleted=request_volume.invalidate_hours, batch=batch
    )


async def test_the_export_takes_the_source_and_names_each_rows_batch():
    pool = _Pool([_row("batch_abc")])
    async with _client(_app(pool)) as client:
        resp = await client.get(f"{EXPORT}&source=batch&batch_id=batch_abc")

    assert resp.status_code == 200, resp.text
    ((sql, args),) = pool.fetches
    assert BATCH in sql
    # $1/$2 are the time range; the batch follows.
    assert "l.batch_job_id = $3" in sql
    assert args[2] == "batch_abc"
    (line,) = resp.text.strip().splitlines()
    assert json.loads(line)["batch_job_id"] == "batch_abc"


async def test_the_export_leaves_out_the_batch_requests_for_interactive():
    pool = _Pool()
    async with _client(_app(pool)) as client:
        resp = await client.get(f"{EXPORT}&source=interactive")

    assert resp.status_code == 200, resp.text
    ((sql, _),) = pool.fetches
    assert INTERACTIVE in sql and BATCH not in sql
