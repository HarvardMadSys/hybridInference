"""The admin Recent Requests total: counted under a time budget, beside the page.

The total is a COUNT(*) over the whole lookback, and under a filter that reads
each row it can take far longer than the page it used to hold up. These pin the
contract that replaced it:

- the count runs with its own ``statement_timeout``;
- past it, ``total`` is a lower bound (``total_exact`` false) and ``has_more``,
  read with the page itself, is what paging goes by;
- a total is reused per filter tuple until it ages out, the admin asks for a
  fresh one, or "Clear errors" deletes rows under it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin import metrics, request_volume


def _row(index: int) -> dict[str, Any]:
    """One page row with every column the list reads."""
    return {
        "request_id": f"req-{index}",
        "user_id": "user-1",
        "user_name": "Ada",
        "user_email": "ada@example.com",
        "model_id": "glm-4.6",
        "provider": "zai",
        "timestamp": datetime(2026, 10, 1, tzinfo=UTC),
        "status_code": 200,
        "latency_ms": 1200,
        "ttft_ms": 300,
        "stream": True,
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
        "user_agent": None,
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
    }


class _FakeDb:
    """Pool and connection doubles: a page of ``page_rows`` rows, a total of ``total``.

    ``total=None`` makes the COUNT raise what Postgres raises when the
    statement timeout cancels it.
    """

    def __init__(self, *, page_rows: int, total: int | None) -> None:
        self.page_rows = page_rows
        self.total = total
        self.counts: list[tuple[str, tuple[Any, ...]]] = []
        self.pages: list[tuple[str, tuple[Any, ...]]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=self._fetchrow)
        conn.fetch = AsyncMock(side_effect=self._fetch)
        conn.execute = AsyncMock(side_effect=self._execute)
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=conn)
        acquire.__aexit__ = AsyncMock(return_value=None)
        self.pool = MagicMock()
        self.pool.acquire = MagicMock(return_value=acquire)

    async def _fetchrow(self, query: str, *args: Any) -> Any:
        self.counts.append((query, args))
        if self.total is None:
            raise asyncpg.QueryCanceledError("canceling statement due to statement timeout")
        return {"total": self.total}

    async def _fetch(self, query: str, *args: Any) -> list[Any]:
        self.pages.append((query, args))
        # The page query binds LIMIT then OFFSET last.
        limit = args[-2]
        return [_row(i) for i in range(min(self.page_rows, limit))]

    async def _execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"


@pytest.fixture(autouse=True)
def _isolate_count_cache():
    metrics._RECENT_REQUESTS_COUNT_CACHE.clear()
    yield
    metrics._RECENT_REQUESTS_COUNT_CACHE.clear()


def _app(db: _FakeDb, **services: Any) -> FastAPI:
    app = FastAPI(title="Admin Recent Requests Total Test")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=RouteExecutor(), db_logger=MagicMock(pool=db.pool), **services
    )
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_total_is_exact_when_the_count_finishes_in_time():
    db = _FakeDb(page_rows=60, total=123)
    async with _client(_app(db)) as client:
        resp = await client.get("/admin/recent-requests?limit=50")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 123
    assert body["total_exact"] is True
    # One row past the page is fetched and dropped: it is how has_more is known.
    assert len(body["requests"]) == 50
    assert body["has_more"] is True
    assert db.pages[0][1][-2:] == (51, 0)
    # The count ran under its own budget, scoped to its transaction.
    assert (
        f"SET LOCAL statement_timeout = {metrics._RECENT_REQUESTS_COUNT_BUDGET_MS}",
        (),
    ) in db.executed


@pytest.mark.asyncio
async def test_a_count_past_its_budget_gives_a_lower_bound_and_has_more():
    db = _FakeDb(page_rows=60, total=None)
    async with _client(_app(db)) as client:
        resp = await client.get("/admin/recent-requests?limit=50&offset=50&outcome=errors")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_exact"] is False
    # The rows through this page: what is known to exist, not a guess.
    assert body["total"] == 100
    assert body["has_more"] is True
    assert len(body["requests"]) == 50


@pytest.mark.asyncio
async def test_the_last_page_has_no_more():
    db = _FakeDb(page_rows=7, total=None)
    async with _client(_app(db)) as client:
        resp = await client.get("/admin/recent-requests?limit=50&offset=100")

    body = resp.json()
    assert body["has_more"] is False
    assert body["total"] == 107


@pytest.mark.asyncio
async def test_a_total_is_counted_once_per_filter_until_refresh():
    db = _FakeDb(page_rows=10, total=10)
    async with _client(_app(db)) as client:
        assert (await client.get("/admin/recent-requests")).status_code == 200
        assert (await client.get("/admin/recent-requests?offset=50")).status_code == 200
        assert len(db.counts) == 1, "paging the same filters must not count again"
        assert len(db.pages) == 2

        assert (await client.get("/admin/recent-requests?refresh=true")).status_code == 200
        assert len(db.counts) == 2, "refresh must count again"

        # A different filter is a different total.
        assert (await client.get("/admin/recent-requests?outcome=errors")).status_code == 200
        assert len(db.counts) == 3


@pytest.mark.asyncio
async def test_a_count_that_ran_out_of_budget_is_not_retried_on_the_next_page():
    db = _FakeDb(page_rows=60, total=None)
    async with _client(_app(db)) as client:
        first = await client.get("/admin/recent-requests?outcome=errors")
        second = await client.get("/admin/recent-requests?outcome=errors&offset=50")

    assert len(db.counts) == 1
    assert first.json()["total_exact"] is False
    assert second.json()["total_exact"] is False
    assert second.json()["total"] == 100


@pytest.mark.asyncio
async def test_clearing_errors_forgets_cached_totals_and_stored_hours():
    db = _FakeDb(page_rows=10, total=10)
    log_store = MagicMock()
    log_store.delete_recent_error_requests = AsyncMock(return_value=4)
    async with _client(_app(db, log_store=log_store)) as client:
        assert (await client.get("/admin/recent-requests")).status_code == 200
        assert metrics._RECENT_REQUESTS_COUNT_CACHE

        resp = await client.post("/admin/recent-requests/clear-errors?hours=3")
        assert resp.status_code == 200, resp.text
        assert not metrics._RECENT_REQUESTS_COUNT_CACHE

        # The next load counts what is left rather than what was there.
        assert (await client.get("/admin/recent-requests")).status_code == 200
        assert len(db.counts) == 2

    # The volume cards' stored hours under the delete are dropped by the delete
    # itself, in its transaction, so the two commit or fail together.
    log_store.delete_recent_error_requests.assert_awaited_once_with(
        hours=3, on_deleted=request_volume.invalidate_hours
    )
    assert not [query for query, _ in db.executed if "request_volume" in query]


@pytest.mark.asyncio
async def test_clearing_nothing_leaves_the_stored_hours_alone():
    db = _FakeDb(page_rows=0, total=0)
    log_store = MagicMock()
    log_store.delete_recent_error_requests = AsyncMock(return_value=0)
    async with _client(_app(db, log_store=log_store)) as client:
        resp = await client.post("/admin/recent-requests/clear-errors")

    assert resp.status_code == 200, resp.text
    assert not [query for query, _ in db.executed if "request_volume" in query]
