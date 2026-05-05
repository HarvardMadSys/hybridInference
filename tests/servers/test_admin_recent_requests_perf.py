"""Tests for /admin/recent-requests time bound + lazy content endpoint.

Verifies the perf-oriented changes from issue #348:

- The list endpoint always binds the timestamp predicate via ``make_interval``
  with a parameterized ``days`` value, default 7, clamped to ``[1, 90]``.
- ``prompt`` / ``response`` are no longer in the list payload.
- New ``GET /admin/recent-requests/{request_id}/content`` endpoint returns
  prompt + response, 404s for missing rows, and rejects unauthenticated callers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _make_db_logger_with_capture() -> tuple[Any, dict[str, list[Any]]]:
    """Mock db_logger whose conn captures every fetch/fetchrow call.

    Returns a (logger, calls) pair where ``calls`` is a dict with keys
    ``"fetchrow"`` and ``"fetch"``. Each value is a list of the arg-tuples
    passed to the corresponding asyncpg method.
    """
    calls: dict[str, list[Any]] = {"fetchrow": [], "fetch": []}

    async def _fetchrow(query: str, *args: Any) -> Any:
        calls["fetchrow"].append((query, args))
        # COUNT(*) query: shape "SELECT COUNT(*) as total FROM api_logs ..."
        if "COUNT(*)" in query:
            return {"total": 0}
        # /content endpoint shape
        if "SELECT prompt, response FROM api_logs" in query:
            # Default: not found; tests override per-call via side_effect when
            # they need a row to come back.
            return None
        return None

    async def _fetch(query: str, *args: Any) -> list[Any]:
        calls["fetch"].append((query, args))
        return []

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

    return logger, calls


@pytest.fixture
def admin_app_with_capture() -> tuple[FastAPI, dict[str, list[Any]], Any]:
    logger, calls = _make_db_logger_with_capture()
    router = RouteExecutor()
    app = FastAPI(title="Admin Recent Requests Perf Test")
    app.state.services = AppServices(router=router, db_logger=logger)  # type: ignore[attr-defined]
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)
    return app, calls, logger


@pytest.fixture
async def admin_client_capture(
    admin_app_with_capture: tuple[FastAPI, dict[str, list[Any]], Any],
) -> AsyncGenerator[tuple[AsyncClient, dict[str, list[Any]], Any], None]:
    app, calls, logger = admin_app_with_capture
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, calls, logger


# ---------------------------------------------------------------------------
# /admin/recent-requests — time bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_default_days_is_seven(admin_client_capture):
    client, calls, _logger = admin_client_capture
    resp = await client.get("/admin/recent-requests")
    assert resp.status_code == 200, resp.text

    # Both COUNT and SELECT must include the make_interval predicate, and the
    # first bound parameter must be 7 (the default lookback).
    assert calls["fetchrow"], "expected COUNT query to run"
    count_query, count_args = calls["fetchrow"][0]
    assert "make_interval(days =>" in count_query
    assert "l.timestamp >= NOW() - make_interval" in count_query
    assert count_args[0] == 7

    assert calls["fetch"], "expected SELECT query to run"
    select_query, select_args = calls["fetch"][0]
    assert "make_interval(days =>" in select_query
    assert select_args[0] == 7

    # And prompt/response must not be in the list SELECT — they are fetched
    # on demand via the /content endpoint. (Match boundaries to avoid false
    # positives on `l.prompt_tokens`.)
    assert "l.prompt," not in select_query
    assert "l.prompt " not in select_query
    assert "l.response," not in select_query
    assert "l.response " not in select_query
    assert "l.metadata->>'ip' AS user_ip" in select_query
    assert "l.metadata->>'peer_ip' AS peer_ip" in select_query
    assert "l.metadata->>'user_agent' AS user_agent" in select_query


@pytest.mark.asyncio
async def test_list_custom_days(admin_client_capture):
    client, calls, _logger = admin_client_capture
    resp = await client.get("/admin/recent-requests?days=30")
    assert resp.status_code == 200, resp.text
    _, count_args = calls["fetchrow"][0]
    _, select_args = calls["fetch"][0]
    assert count_args[0] == 30
    assert select_args[0] == 30


@pytest.mark.asyncio
async def test_list_days_clamped_high(admin_client_capture):
    client, calls, _logger = admin_client_capture
    resp = await client.get("/admin/recent-requests?days=200")
    assert resp.status_code == 200, resp.text
    _, count_args = calls["fetchrow"][0]
    _, select_args = calls["fetch"][0]
    assert count_args[0] == 90
    assert select_args[0] == 90


@pytest.mark.asyncio
async def test_list_days_clamped_low(admin_client_capture):
    client, calls, _logger = admin_client_capture
    resp = await client.get("/admin/recent-requests?days=0")
    assert resp.status_code == 200, resp.text
    _, count_args = calls["fetchrow"][0]
    _, select_args = calls["fetch"][0]
    assert count_args[0] == 1
    assert select_args[0] == 1


@pytest.mark.asyncio
async def test_list_response_omits_prompt_and_response(admin_client_capture):
    """Even if a row carried prompt/response by accident, the model strips them."""
    client, _calls, logger = admin_client_capture

    # Replace the conn.fetch side effect to return a fully-populated row that
    # *does* include prompt/response keys, to prove the response model drops
    # them rather than relying on the SQL alone.
    async def _fake_fetch(query: str, *_args: Any) -> list[Any]:
        if "FROM api_logs l" in query:
            return [
                {
                    "request_id": "req-1",
                    "user_id": "user-1",
                    "user_name": "u",
                    "user_email": "u@example.com",
                    "model_id": "m",
                    "provider": "p",
                    "timestamp": datetime.now(timezone.utc),
                    "status_code": 200,
                    "latency_ms": 100,
                    "ttft_ms": 10,
                    "stream": False,
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "reasoning_tokens": None,
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                    "total_tokens": 2,
                    "cost_usd": None,
                    "error": None,
                    "user_ip": None,
                    "peer_ip": "172.19.0.8",
                    "ip_source": "x-forwarded-for",
                    "x_forwarded_for": "203.0.113.8",
                    "user_agent": "pytest-client",
                    "session_id": "sess-1",
                    "request_surface": "openai_chat_completions",
                    # Deliberately seed prompt/response into the row to prove
                    # the response model strips them; if a future regression
                    # reintroduces them on AdminRecentRequestItem, this test
                    # will catch it.
                    "prompt": "should-not-appear",
                    "response": "should-not-appear",
                }
            ]
        return []

    conn = await logger.pool.acquire().__aenter__()
    conn.fetch = AsyncMock(side_effect=_fake_fetch)

    resp = await client.get("/admin/recent-requests")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requests"], body
    item = body["requests"][0]
    assert "prompt" not in item
    assert "response" not in item


# ---------------------------------------------------------------------------
# /admin/recent-requests/{id}/content — lazy content fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_returns_prompt_and_response(admin_client_capture):
    client, _calls, logger = admin_client_capture
    conn = await logger.pool.acquire().__aenter__()

    async def _fake_fetchrow(query: str, *_args: Any) -> Any:
        if "SELECT prompt, response FROM api_logs" in query:
            return {"prompt": "hello", "response": "world"}
        return None

    conn.fetchrow = AsyncMock(side_effect=_fake_fetchrow)

    resp = await client.get("/admin/recent-requests/req-abc/content")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"prompt": "hello", "response": "world", "reasoning_content": None}


@pytest.mark.asyncio
async def test_content_returns_404_when_missing(admin_client_capture):
    client, _calls, _logger = admin_client_capture
    # Default mock fetchrow returns None for the content query → 404.
    resp = await client.get("/admin/recent-requests/does-not-exist/content")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_content_query_uses_request_id_param(admin_client_capture):
    """The /content endpoint must pass the request_id as a bound parameter,
    not interpolate it into the SQL string."""
    client, calls, _logger = admin_client_capture
    resp = await client.get("/admin/recent-requests/req-xyz/content")
    assert resp.status_code in (200, 404)
    content_calls = [
        (q, a) for q, a in calls["fetchrow"] if "SELECT prompt, response FROM api_logs" in q
    ]
    assert content_calls, "expected the /content fetchrow to be issued"
    query, args = content_calls[0]
    assert "WHERE request_id = $1" in query
    assert args == ("req-xyz",)


# ---------------------------------------------------------------------------
# Auth gating on the new endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_requires_admin_auth():
    """Without an admin token the endpoint must reject the caller."""
    logger, _calls = _make_db_logger_with_capture()
    router = RouteExecutor()
    app = FastAPI(title="Admin Recent Requests Auth Test")
    app.state.services = AppServices(router=router, db_logger=logger)  # type: ignore[attr-defined]
    # Note: NO override of verify_admin_access here, so the real dependency runs.
    app.include_router(admin.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/recent-requests/anything/content")
    assert resp.status_code in (401, 403), resp.text
