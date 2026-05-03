"""Unit tests for the recent-requests COUNT(*) TTL cache.

Exercises ``_get_cached_user_request_count`` in
``serving.servers.routers.user_routes`` against a mocked asyncpg-style
connection. The helper guards the dashboard's hot path so we verify:

- Cache miss runs the COUNT exactly once.
- A subsequent call within TTL returns the cached value without querying.
- After the cached entry's timestamp ages past the TTL, the COUNT runs again.
- Distinct ``(user_id, model_id)`` keys are cached independently.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from serving.servers.routers import user_routes
from serving.servers.routers.user_routes import (
    _RECENT_REQUESTS_COUNT_CACHE,
    _RECENT_REQUESTS_COUNT_TTL_SECONDS,
    _get_cached_user_request_count,
)


@pytest.fixture(autouse=True)
def _reset_recent_requests_count_cache():
    """Clear the module-level cache + lock between tests."""
    _RECENT_REQUESTS_COUNT_CACHE.clear()
    user_routes._RECENT_REQUESTS_COUNT_LOCK = asyncio.Lock()
    yield
    _RECENT_REQUESTS_COUNT_CACHE.clear()
    user_routes._RECENT_REQUESTS_COUNT_LOCK = asyncio.Lock()


def _make_conn(total: int = 7) -> AsyncMock:
    """Return a mock asyncpg connection whose ``fetchrow`` yields a COUNT row."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": total})
    return conn


@pytest.mark.asyncio
async def test_cache_miss_executes_count_once():
    """First call against an empty cache runs the COUNT and returns its value."""
    conn = _make_conn(total=42)

    total = await _get_cached_user_request_count(conn, "user-1", None)

    assert total == 42
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_cache_hit_skips_count_query():
    """Second call inside the TTL window must not re-issue the COUNT."""
    conn = _make_conn(total=42)

    first = await _get_cached_user_request_count(conn, "user-1", None)
    second = await _get_cached_user_request_count(conn, "user-1", None)

    assert first == 42
    assert second == 42
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_count_reruns_after_ttl_expiry():
    """Once the cached timestamp ages past the TTL, the COUNT runs again."""
    conn = _make_conn(total=42)

    await _get_cached_user_request_count(conn, "user-1", None)
    assert conn.fetchrow.await_count == 1

    # Age the cached entry past the TTL without sleeping.
    cached_ts, cached_total = _RECENT_REQUESTS_COUNT_CACHE[("user-1", None)]
    _RECENT_REQUESTS_COUNT_CACHE[("user-1", None)] = (
        cached_ts - _RECENT_REQUESTS_COUNT_TTL_SECONDS - 1.0,
        cached_total,
    )
    conn.fetchrow.return_value = {"total": 99}

    refreshed = await _get_cached_user_request_count(conn, "user-1", None)

    assert refreshed == 99
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_distinct_keys_cache_independently():
    """Different (user_id, model_id) tuples each get their own cache entry."""
    conn = _make_conn()
    conn.fetchrow.side_effect = [
        {"total": 10},
        {"total": 20},
        {"total": 30},
    ]

    a = await _get_cached_user_request_count(conn, "user-1", None)
    b = await _get_cached_user_request_count(conn, "user-1", "gpt-4o")
    c = await _get_cached_user_request_count(conn, "user-2", None)

    assert (a, b, c) == (10, 20, 30)
    assert conn.fetchrow.await_count == 3

    # Subsequent hits all served from cache.
    a2 = await _get_cached_user_request_count(conn, "user-1", None)
    b2 = await _get_cached_user_request_count(conn, "user-1", "gpt-4o")
    c2 = await _get_cached_user_request_count(conn, "user-2", None)

    assert (a2, b2, c2) == (10, 20, 30)
    assert conn.fetchrow.await_count == 3


@pytest.mark.asyncio
async def test_count_query_filters_on_model_id_when_provided():
    """When ``model_id`` is supplied the helper passes both bind params."""
    conn = _make_conn(total=5)

    await _get_cached_user_request_count(conn, "user-1", "gpt-4o")

    assert conn.fetchrow.await_count == 1
    args, _kwargs = conn.fetchrow.call_args
    sql = args[0]
    bind_params = args[1:]
    assert "user_id = $1" in sql
    assert "model_id = $2" in sql
    assert bind_params == ("user-1", "gpt-4o")


@pytest.mark.asyncio
async def test_count_query_omits_model_id_when_none():
    """Without ``model_id`` only the user bind param is passed."""
    conn = _make_conn(total=5)

    await _get_cached_user_request_count(conn, "user-1", None)

    args, _kwargs = conn.fetchrow.call_args
    sql = args[0]
    bind_params = args[1:]
    assert "user_id = $1" in sql
    assert "model_id" not in sql
    assert bind_params == ("user-1",)
