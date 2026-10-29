"""Unit tests for the recent-requests COUNT(*) TTL cache and presentation.

Exercises ``get_cached_user_request_count``, ``query_recent_requests`` and
``present_recent_requests`` in ``serving.servers.routers.recent_requests``
against a mocked asyncpg-style connection. The helper guards the dashboard's hot
path so we verify:

- Cache miss runs the COUNT exactly once.
- A subsequent call within TTL returns the cached value without querying.
- After the cached entry's timestamp ages past the TTL, the COUNT runs again.
- Distinct ``(user_id, model_id)`` keys are cached independently.
- The cache evicts oldest entries once the size cap is reached.
- The page query keeps its bind order and pagination.
- An auto-model row never reveals the model that served it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.exceptions import generic_message_for_status, user_safe_error_for_log
from serving.servers.routers import recent_requests, user_routes
from serving.servers.routers.recent_requests import (
    RECENT_REQUESTS_COUNT_CACHE,
    RECENT_REQUESTS_COUNT_TTL_SECONDS,
    SHOWN_MODEL_SQL,
    get_cached_user_request_count,
    present_recent_requests,
    query_recent_requests,
)


@pytest.fixture(autouse=True)
def _reset_recent_requests_count_cache():
    """Clear the module-level cache between tests."""
    RECENT_REQUESTS_COUNT_CACHE.clear()
    yield
    RECENT_REQUESTS_COUNT_CACHE.clear()


def _make_conn(total: int = 7) -> AsyncMock:
    """Return a mock asyncpg connection whose ``fetchrow`` yields a COUNT row."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": total})
    return conn


@pytest.mark.asyncio
async def test_cache_miss_executes_count_once():
    """First call against an empty cache runs the COUNT and returns its value."""
    conn = _make_conn(total=42)

    total = await get_cached_user_request_count(conn, "user-1", None)

    assert total == 42
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_cache_hit_skips_count_query():
    """Second call inside the TTL window must not re-issue the COUNT."""
    conn = _make_conn(total=42)

    first = await get_cached_user_request_count(conn, "user-1", None)
    second = await get_cached_user_request_count(conn, "user-1", None)

    assert first == 42
    assert second == 42
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_count_reruns_after_ttl_expiry():
    """Once the cached timestamp ages past the TTL, the COUNT runs again."""
    conn = _make_conn(total=42)

    await get_cached_user_request_count(conn, "user-1", None)
    assert conn.fetchrow.await_count == 1

    # Age the cached entry past the TTL without sleeping.
    cached_ts, cached_total = RECENT_REQUESTS_COUNT_CACHE[("user-1", None)]
    RECENT_REQUESTS_COUNT_CACHE[("user-1", None)] = (
        cached_ts - RECENT_REQUESTS_COUNT_TTL_SECONDS - 1.0,
        cached_total,
    )
    conn.fetchrow.return_value = {"total": 99}

    refreshed = await get_cached_user_request_count(conn, "user-1", None)

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

    a = await get_cached_user_request_count(conn, "user-1", None)
    b = await get_cached_user_request_count(conn, "user-1", "gpt-4o")
    c = await get_cached_user_request_count(conn, "user-2", None)

    assert (a, b, c) == (10, 20, 30)
    assert conn.fetchrow.await_count == 3

    # Subsequent hits all served from cache.
    a2 = await get_cached_user_request_count(conn, "user-1", None)
    b2 = await get_cached_user_request_count(conn, "user-1", "gpt-4o")
    c2 = await get_cached_user_request_count(conn, "user-2", None)

    assert (a2, b2, c2) == (10, 20, 30)
    assert conn.fetchrow.await_count == 3


@pytest.mark.asyncio
async def test_count_query_filters_on_model_id_when_provided():
    """When ``model_id`` is supplied the helper uses case-insensitive partial matching."""
    conn = _make_conn(total=5)

    await get_cached_user_request_count(conn, "user-1", "gpt-4o")

    assert conn.fetchrow.await_count == 1
    args, _kwargs = conn.fetchrow.call_args
    sql = args[0]
    bind_params = args[1:]
    assert "user_id = $1" in sql
    assert f"{SHOWN_MODEL_SQL} ILIKE '%' || $2 || '%'" in sql
    assert bind_params == ("user-1", "gpt-4o")


@pytest.mark.asyncio
async def test_count_query_escapes_ilike_wildcards_in_model_id():
    """Literal ``%`` and ``_`` in ``model_id`` are escaped before ILIKE matching."""
    conn = _make_conn(total=5)

    await get_cached_user_request_count(conn, "user-1", r"gpt%_4\\o")

    assert conn.fetchrow.await_count == 1
    args, _kwargs = conn.fetchrow.call_args
    sql = args[0]
    bind_params = args[1:]
    assert "user_id = $1" in sql
    assert f"{SHOWN_MODEL_SQL} ILIKE '%' || $2 || '%' ESCAPE '\\'" in sql
    assert bind_params == ("user-1", r"gpt\%\_4\\\\o")


@pytest.mark.asyncio
async def test_count_query_omits_model_id_when_none():
    """Without ``model_id`` only the user bind param is passed."""
    conn = _make_conn(total=5)

    await get_cached_user_request_count(conn, "user-1", None)

    args, _kwargs = conn.fetchrow.call_args
    sql = args[0]
    bind_params = args[1:]
    assert "user_id = $1" in sql
    assert "model_id" not in sql
    assert bind_params == ("user-1",)


@pytest.mark.asyncio
async def test_cache_evicts_oldest_entries_at_capacity(monkeypatch):
    """Once the cache hits its size cap, the LRU entry is evicted."""
    monkeypatch.setattr(recent_requests, "RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES", 2)
    conn = _make_conn()
    conn.fetchrow.side_effect = [{"total": i} for i in range(10)]

    await get_cached_user_request_count(conn, "user-1", None)
    await get_cached_user_request_count(conn, "user-2", None)
    assert set(RECENT_REQUESTS_COUNT_CACHE.keys()) == {("user-1", None), ("user-2", None)}

    # Inserting a third distinct key evicts the least-recently-used entry.
    await get_cached_user_request_count(conn, "user-3", None)
    assert set(RECENT_REQUESTS_COUNT_CACHE.keys()) == {("user-2", None), ("user-3", None)}

    # Reading user-2 promotes it; user-3 is now LRU.
    await get_cached_user_request_count(conn, "user-2", None)
    await get_cached_user_request_count(conn, "user-4", None)
    assert set(RECENT_REQUESTS_COUNT_CACHE.keys()) == {("user-2", None), ("user-4", None)}


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
    assert f"{SHOWN_MODEL_SQL} AS model_id" in sql
    named, auto = page.requests
    assert (named.model_id, named.provider, named.routewise) == (
        "mid-b",
        "zai",
        {"primary_provider": "mid-b:zai-api"},
    )
    assert (auto.model_id, auto.provider, auto.routewise) == ("auto-balance", "auto", None)
    assert "mid-b" not in (auto.error or "")


@pytest.mark.asyncio
async def test_the_page_query_binds_filters_before_pagination():
    """One filter list feeds both queries; only the page is paginated."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": 3})
    conn.fetch = AsyncMock(return_value=[])

    rows, total = await query_recent_requests(
        conn, user_id="user-1", model_id="gpt-4o", limit=25, offset=50
    )

    assert (rows, total) == ([], 3)
    count_sql, *count_params = conn.fetchrow.call_args.args
    assert "COUNT(*)" in count_sql
    assert count_params == ["user-1", "gpt-4o"]

    page_sql, *page_params = conn.fetch.call_args.args
    assert f"{SHOWN_MODEL_SQL} AS model_id" in page_sql
    assert "ORDER BY timestamp DESC" in page_sql
    assert "LIMIT $3 OFFSET $4" in page_sql
    assert page_params == ["user-1", "gpt-4o", 25, 50]


@pytest.mark.asyncio
async def test_the_page_query_counts_before_it_reads_a_page():
    """A failing COUNT must not be followed by a page read."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("count failed"))
    conn.fetch = AsyncMock()

    with pytest.raises(RuntimeError):
        await query_recent_requests(conn, user_id="user-1", model_id=None, limit=50, offset=0)

    conn.fetch.assert_not_awaited()


def test_a_named_row_keeps_its_provider_routing_and_sanitized_error():
    raw_error = "upstream returned 429 for provider zai"

    (item,) = present_recent_requests([_row(error=raw_error, status_code=429)])

    assert (item.provider, item.routewise) == ("zai", {"primary_provider": "mid-b:zai-api"})
    assert item.error == user_safe_error_for_log(raw_error, 429)


def test_an_auto_row_withholds_served_provider_routing_and_raw_error():
    """Auto-model rows are the privacy-sensitive case; none of these may leak."""
    (item,) = present_recent_requests(
        [
            _row(
                model_id="auto-balance",
                status_code=503,
                error="No provider is currently available for model 'mid-b'",
                routewise={"primary_provider": "mid-b:zai-api"},
                is_auto=True,
            )
        ]
    )

    assert item.model_id == "auto-balance"
    assert (item.provider, item.routewise) == ("auto", None)
    assert item.error == generic_message_for_status(503)
    assert "mid-b" not in (item.error or "")


def test_presentation_coerces_cost_to_json_safe_numbers():
    (priced, unpriced) = present_recent_requests(
        [_row(cost_usd=Decimal("0.002")), _row(cost_usd=None)]
    )

    assert priced.cost_usd == 0.002
    assert isinstance(priced.cost_usd, float)
    assert unpriced.cost_usd is None


def test_presentation_of_an_empty_page_is_empty():
    assert present_recent_requests([]) == []
