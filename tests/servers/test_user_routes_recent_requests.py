"""Unit tests for the recent-requests COUNT(*) TTL cache.

Exercises ``_get_cached_user_request_count`` in
``serving.servers.routers.user_routes`` against a mocked asyncpg-style
connection. The helper guards the dashboard's hot path so we verify:

- Cache miss runs the COUNT exactly once.
- A subsequent call within TTL returns the cached value without querying.
- After the cached entry's timestamp ages past the TTL, the COUNT runs again.
- Distinct ``(user_id, model_id)`` keys are cached independently.
- The cache evicts oldest entries once the size cap is reached.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers import user_routes
from serving.servers.routers.user_routes import (
    _RECENT_REQUESTS_COUNT_CACHE,
    _RECENT_REQUESTS_COUNT_TTL_SECONDS,
    _SHOWN_MODEL_SQL,
    _get_cached_user_request_count,
)


@pytest.fixture(autouse=True)
def _reset_recent_requests_count_cache():
    """Clear the module-level cache between tests."""
    _RECENT_REQUESTS_COUNT_CACHE.clear()
    yield
    _RECENT_REQUESTS_COUNT_CACHE.clear()


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
    """When ``model_id`` is supplied the helper uses case-insensitive partial matching."""
    conn = _make_conn(total=5)

    await _get_cached_user_request_count(conn, "user-1", "gpt-4o")

    assert conn.fetchrow.await_count == 1
    args, _kwargs = conn.fetchrow.call_args
    sql = args[0]
    bind_params = args[1:]
    assert "user_id = $1" in sql
    assert f"{_SHOWN_MODEL_SQL} ILIKE '%' || $2 || '%'" in sql
    assert bind_params == ("user-1", "gpt-4o")


@pytest.mark.asyncio
async def test_count_query_escapes_ilike_wildcards_in_model_id():
    """Literal ``%`` and ``_`` in ``model_id`` are escaped before ILIKE matching."""
    conn = _make_conn(total=5)

    await _get_cached_user_request_count(conn, "user-1", r"gpt%_4\\o")

    assert conn.fetchrow.await_count == 1
    args, _kwargs = conn.fetchrow.call_args
    sql = args[0]
    bind_params = args[1:]
    assert "user_id = $1" in sql
    assert f"{_SHOWN_MODEL_SQL} ILIKE '%' || $2 || '%' ESCAPE '\\'" in sql
    assert bind_params == ("user-1", r"gpt\%\_4\\\\o")


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


@pytest.mark.asyncio
async def test_cache_evicts_oldest_entries_at_capacity(monkeypatch):
    """Once the cache hits its size cap, the LRU entry is evicted."""
    monkeypatch.setattr(user_routes, "_RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES", 2)
    conn = _make_conn()
    conn.fetchrow.side_effect = [{"total": i} for i in range(10)]

    await _get_cached_user_request_count(conn, "user-1", None)
    await _get_cached_user_request_count(conn, "user-2", None)
    assert set(_RECENT_REQUESTS_COUNT_CACHE.keys()) == {("user-1", None), ("user-2", None)}

    # Inserting a third distinct key evicts the least-recently-used entry.
    await _get_cached_user_request_count(conn, "user-3", None)
    assert set(_RECENT_REQUESTS_COUNT_CACHE.keys()) == {("user-2", None), ("user-3", None)}

    # Reading user-2 promotes it; user-3 is now LRU.
    await _get_cached_user_request_count(conn, "user-2", None)
    await _get_cached_user_request_count(conn, "user-4", None)
    assert set(_RECENT_REQUESTS_COUNT_CACHE.keys()) == {("user-2", None), ("user-4", None)}


def _row(**overrides):
    return {
        "request_id": "req-1",
        "model_id": "mid-b",
        "provider": "zai",
        "timestamp": datetime(2026, 10, 15, tzinfo=UTC),
        "status_code": 200,
        "latency_ms": 10,
        "ttft_ms": 5,
        "stream": False,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 2,
        "cost_usd": 0.001,
        "error": None,
        "routewise": {"primary_provider": "mid-b:zai-api"},
        "request_type": None,
        "is_auto": False,
        **overrides,
    }


@pytest.mark.asyncio
async def test_a_request_for_an_auto_model_shows_the_auto_model_only():
    """The query names the auto model; the row withholds what would name the served one."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": 2})
    conn.fetch = AsyncMock(
        return_value=[
            _row(),
            _row(
                request_id="req-2",
                model_id="auto-balance",
                status_code=503,
                error="No provider is currently available for model 'mid-b'",
                is_auto=True,
            ),
        ]
    )
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    db_logger = SimpleNamespace(pool=SimpleNamespace(acquire=lambda: acquire))

    page = await user_routes.get_recent_requests(
        limit=50, offset=0, model_id=None, current_user={"user_id": "user-1"}, db_logger=db_logger
    )

    sql = conn.fetch.call_args.args[0]
    assert f"{_SHOWN_MODEL_SQL} AS model_id" in sql
    named, auto = page.requests
    assert (named.model_id, named.provider, named.routewise) == (
        "mid-b",
        "zai",
        {"primary_provider": "mid-b:zai-api"},
    )
    assert (auto.model_id, auto.provider, auto.routewise) == ("auto-balance", "auto", None)
    assert "mid-b" not in (auto.error or "")
