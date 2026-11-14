"""The admin request export outlives the request timeout.

``TimeoutMiddleware`` ends a response at ``REQUEST_TIMEOUT_SECONDS`` even after
its ``200`` went out, unless the response is a stream. A JSONL export of many
requests is one: the route marks itself so, and gets the stream cap instead.
Here the request timeout is a fraction of a second and the database serves a
page every 0.1 s, so an unmarked export would end short.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import get_admin_audit_actor, get_db_logger
from serving.servers.middleware.timeout import TimeoutMiddleware
from serving.servers.routers.admin import export
from tests.fixtures.audit import ADMIN_ACTOR

#: The route's page size: a page this long is followed by another.
PAGE_ROWS = 500
PAGES = 6
PAGE_DELAY = 0.1
REQUEST_TIMEOUT = 0.25
EXPORT_URL = "/admin/export/requests?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z"


def _row(number: int) -> dict[str, Any]:
    return {
        "request_id": f"req-{number}",
        "user_id": "user-1",
        "user_name": "alice",
        "user_email": "alice@example.com",
        "model_id": "m",
        "provider": "p",
        "timestamp": datetime(2024, 6, 1, tzinfo=UTC) - timedelta(seconds=number),
        "status_code": 200,
        "latency_ms": 1,
        "ttft_ms": 1,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "total_tokens": 2,
        "cost_usd": None,
        "error": None,
        "batch_job_id": None,
    }


def _slow_db() -> MagicMock:
    """A request log whose every page takes ``PAGE_DELAY`` to come back."""
    pages = iter(
        [[_row(page * PAGE_ROWS + i) for i in range(PAGE_ROWS)] for page in range(PAGES - 1)]
        + [[_row((PAGES - 1) * PAGE_ROWS)]]
    )

    async def fetch(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(PAGE_DELAY)
        return next(pages, [])

    conn = MagicMock()
    conn.fetch = fetch
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=conn)
    acquired.__aexit__ = AsyncMock(return_value=None)
    db = MagicMock()
    db.pool.acquire = MagicMock(return_value=acquired)
    db.log_admin_action = AsyncMock()
    return db


@pytest.fixture
def app() -> TimeoutMiddleware:
    application = FastAPI()
    db = _slow_db()
    application.dependency_overrides[get_admin_audit_actor] = lambda: ADMIN_ACTOR
    application.dependency_overrides[get_db_logger] = lambda: db
    application.include_router(export.router)
    return TimeoutMiddleware(application, timeout_s=REQUEST_TIMEOUT, stream_timeout_s=30)


@pytest.mark.asyncio
async def test_an_export_slower_than_the_request_timeout_is_served_whole(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(EXPORT_URL)

    assert response.status_code == 200
    lines = response.text.splitlines()
    assert len(lines) == (PAGES - 1) * PAGE_ROWS + 1
    # Whole to the last row, and each line a record of its own.
    assert json.loads(lines[-1])["request_id"] == f"req-{(PAGES - 1) * PAGE_ROWS}"


@pytest.mark.asyncio
async def test_a_route_that_does_not_stream_still_times_out(app):
    """The mark is per response: anything else keeps the request timeout."""
    inner = app.app

    @inner.get("/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(1)
        return {"ok": "late"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/slow")

    assert response.status_code == 504
