"""Unit tests for ``/user/recent-requests``: its count cache and its bounds.

Exercises ``get_cached_user_request_count`` in
``serving.servers.routers.recent_requests`` and ``get_recent_requests`` in
``serving.servers.routers.user_routes`` against a mocked asyncpg-style
connection. We verify:

- Cache miss runs the COUNT exactly once.
- A subsequent call within TTL returns the cached value without querying.
- After the cached entry's timestamp ages past the TTL, the COUNT runs again.
- Distinct ``(user_id, key)`` entries are cached independently; a count by
  model, whatever the caller typed, is not cached at all.
- The cache evicts oldest entries once the size cap is reached.
- Every read reaches back over the account's newest ``RECENT_REQUESTS_WINDOW``
  rows only, under a statement timeout, and the offset stays inside them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers import recent_requests, user_routes
from serving.servers.routers.recent_requests import (
    _SHOWN_MODEL_SQL,
    RECENT_REQUESTS_COUNT_TTL_SECONDS,
    RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC,
    RECENT_REQUESTS_WINDOW,
    get_cached_user_request_count,
)
from serving.storage.pools import DatabaseBusyError


@pytest.fixture(autouse=True)
def _reset_recent_requests_count_cache():
    """Clear the module-level cache between tests."""
    recent_requests.RECENT_REQUESTS_COUNT_CACHE.clear()
    yield
    recent_requests.RECENT_REQUESTS_COUNT_CACHE.clear()


def _make_conn(total: int = 7, capped: bool = False) -> AsyncMock:
    """Return a mock asyncpg connection whose ``fetchrow`` yields a COUNT row."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": total, "capped": capped})
    return conn


@pytest.mark.asyncio
async def test_cache_miss_executes_count_once():
    """First call against an empty cache runs the COUNT and returns its value."""
    conn = _make_conn(total=42)

    total = await get_cached_user_request_count(conn, "user-1", None)

    assert total == (42, False)
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_cache_hit_skips_count_query():
    """Second call inside the TTL window must not re-issue the COUNT."""
    conn = _make_conn(total=42)

    first = await get_cached_user_request_count(conn, "user-1", None)
    second = await get_cached_user_request_count(conn, "user-1", None)

    assert first == (42, False)
    assert second == (42, False)
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_count_reruns_after_ttl_expiry():
    """Once the cached timestamp ages past the TTL, the COUNT runs again."""
    conn = _make_conn(total=42)

    await get_cached_user_request_count(conn, "user-1", None)
    assert conn.fetchrow.await_count == 1

    # Age the cached entry past the TTL without sleeping.
    cached_ts, cached_total = recent_requests.RECENT_REQUESTS_COUNT_CACHE[("user-1", None)]
    recent_requests.RECENT_REQUESTS_COUNT_CACHE[("user-1", None)] = (
        cached_ts - RECENT_REQUESTS_COUNT_TTL_SECONDS - 1.0,
        cached_total,
    )
    conn.fetchrow.return_value = {"total": 99, "capped": True}

    refreshed = await get_cached_user_request_count(conn, "user-1", None)

    assert refreshed == (99, True)
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_distinct_keys_cache_independently():
    """Each (user, key) gets its own cache entry; a count by model is never cached."""
    conn = _make_conn()
    conn.fetchrow.side_effect = [{"total": n, "capped": False} for n in (10, 20, 30, 40, 21)]

    a = await get_cached_user_request_count(conn, "user-1", None)
    b = await get_cached_user_request_count(conn, "user-1", "gpt-4o")
    c = await get_cached_user_request_count(conn, "user-2", None)
    d = await get_cached_user_request_count(conn, "user-1", None, 7)

    assert [n for n, _ in (a, b, c, d)] == [10, 20, 30, 40]
    assert conn.fetchrow.await_count == 4

    # Subsequent hits served from cache, but the model's count runs again.
    a2 = await get_cached_user_request_count(conn, "user-1", None)
    b2 = await get_cached_user_request_count(conn, "user-1", "gpt-4o")
    c2 = await get_cached_user_request_count(conn, "user-2", None)
    d2 = await get_cached_user_request_count(conn, "user-1", None, 7)

    assert [n for n, _ in (a2, b2, c2, d2)] == [10, 21, 30, 40]
    assert conn.fetchrow.await_count == 5
    assert set(recent_requests.RECENT_REQUESTS_COUNT_CACHE) == {
        ("user-1", None),
        ("user-2", None),
        ("user-1", 7),
    }


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
    assert f"{_SHOWN_MODEL_SQL} ILIKE '%' || $2 || '%'" in sql
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
    assert f"{_SHOWN_MODEL_SQL} ILIKE '%' || $2 || '%' ESCAPE '\\'" in sql
    assert bind_params == ("user-1", r"gpt\%\_4\\\\o")


@pytest.mark.asyncio
async def test_the_count_reaches_back_over_the_window_only():
    """However much history the account has, the count reads its newest rows only."""
    conn = _make_conn(total=RECENT_REQUESTS_WINDOW, capped=True)

    counted = await get_cached_user_request_count(conn, "user-1", "gpt-4o", 7)

    assert counted == (RECENT_REQUESTS_WINDOW, True)
    sql = " ".join(conn.fetchrow.call_args.args[0].split())
    assert (
        "FROM api_logs WHERE user_id = $1 ORDER BY timestamp DESC, cost_usd DESC "
        f"LIMIT {RECENT_REQUESTS_WINDOW}) AS recent" in sql
    )
    assert f"OFFSET {RECENT_REQUESTS_WINDOW}) AS capped" in sql


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
    conn.fetchrow.side_effect = [{"total": i, "capped": False} for i in range(10)]

    await get_cached_user_request_count(conn, "user-1", None)
    await get_cached_user_request_count(conn, "user-2", None)
    assert set(recent_requests.RECENT_REQUESTS_COUNT_CACHE.keys()) == {
        ("user-1", None),
        ("user-2", None),
    }

    # Inserting a third distinct key evicts the least-recently-used entry.
    await get_cached_user_request_count(conn, "user-3", None)
    assert set(recent_requests.RECENT_REQUESTS_COUNT_CACHE.keys()) == {
        ("user-2", None),
        ("user-3", None),
    }

    # Reading user-2 promotes it; user-3 is now LRU.
    await get_cached_user_request_count(conn, "user-2", None)
    await get_cached_user_request_count(conn, "user-4", None)
    assert set(recent_requests.RECENT_REQUESTS_COUNT_CACHE.keys()) == {
        ("user-2", None),
        ("user-4", None),
    }


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
        "api_key_id": None,
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
    page = await user_routes.get_recent_requests(
        limit=50,
        offset=0,
        model_id=None,
        key_prefix=None,
        current_user={"user_id": "user-1"},
        db_logger=_db_logger(conn),
        op_store=None,
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


def _db_logger(conn):
    """A logger whose pool hands out *conn*, with the transaction the reads run in."""
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    return SimpleNamespace(pool=SimpleNamespace(acquire=lambda: acquire))


def _keys_store(*keys: tuple[int, str]):
    store = MagicMock()
    store.list_account_keys = AsyncMock(
        return_value=[{"id": key_id, "key_prefix": prefix} for key_id, prefix in keys]
    )
    return store


@pytest.mark.asyncio
async def test_requests_name_the_key_they_were_made_with():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": 3})
    conn.fetch = AsyncMock(
        return_value=[
            _row(request_id="by-laptop", api_key_id="7"),
            _row(request_id="by-gone-key", api_key_id="99"),
            _row(request_id="before-attribution"),
        ]
    )

    page = await user_routes.get_recent_requests(
        limit=50,
        offset=0,
        model_id=None,
        key_prefix=None,
        current_user={"user_id": "user-keys"},
        db_logger=_db_logger(conn),
        op_store=_keys_store((7, "hyi-laptop00")),
    )

    assert [r.key_prefix for r in page.requests] == ["hyi-laptop00", None, None]


@pytest.mark.asyncio
async def test_filtering_by_key_reads_that_keys_rows_of_the_account():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": 1})
    conn.fetch = AsyncMock(return_value=[_row(api_key_id="8")])

    await user_routes.get_recent_requests(
        limit=50,
        offset=0,
        model_id=None,
        key_prefix="hyi-ci000000",
        current_user={"user_id": "user-filter"},
        db_logger=_db_logger(conn),
        op_store=_keys_store((7, "hyi-laptop00"), (8, "hyi-ci000000")),
    )

    sql, *params = conn.fetch.call_args.args
    assert "user_id = $1" in sql and "metadata->>'api_key_id' = $2" in sql
    assert params[:2] == ["user-filter", "8"]


@pytest.mark.asyncio
async def test_filtering_by_a_key_the_account_does_not_hold_is_404():
    from fastapi import HTTPException

    conn = AsyncMock()
    with pytest.raises(HTTPException) as refused:
        await user_routes.get_recent_requests(
            limit=50,
            offset=0,
            model_id=None,
            key_prefix="hyi-someone00",
            current_user={"user_id": "user-404"},
            db_logger=_db_logger(conn),
            op_store=_keys_store((7, "hyi-laptop00")),
        )

    assert refused.value.status_code == 404
    conn.fetch.assert_not_awaited()


async def _page(conn, *, offset=0, limit=50, model_id=None):
    return await user_routes.get_recent_requests(
        limit=limit,
        offset=offset,
        model_id=model_id,
        key_prefix=None,
        current_user={"user_id": "user-bounds"},
        db_logger=_db_logger(conn),
        op_store=None,
    )


@pytest.mark.asyncio
async def test_a_page_is_read_from_the_window_under_a_timeout():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": RECENT_REQUESTS_WINDOW, "capped": True})
    conn.fetch = AsyncMock(return_value=[_row()])

    page = await _page(conn)

    assert (page.total, page.total_capped) == (RECENT_REQUESTS_WINDOW, True)
    sql = " ".join(conn.fetch.call_args.args[0].split())
    assert (
        "FROM api_logs WHERE user_id = $1 ORDER BY timestamp DESC, cost_usd DESC "
        f"LIMIT {RECENT_REQUESTS_WINDOW}) AS recent" in sql
    )
    # Both reads run in one read-only transaction that set the timeout and the plan.
    conn.transaction.assert_called_once_with(readonly=True)
    (settings,) = conn.execute.await_args.args
    timeout_ms = int(RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC * 1000)
    assert f"SET LOCAL statement_timeout = '{timeout_ms}ms'" in settings
    assert "SET LOCAL enable_sort = off" in settings
    assert "SET LOCAL enable_bitmapscan = off" in settings


@pytest.mark.asyncio
async def test_an_offset_past_the_window_is_refused_before_any_read():
    from fastapi import HTTPException

    conn = AsyncMock()
    with pytest.raises(HTTPException) as refused:
        await _page(conn, offset=RECENT_REQUESTS_WINDOW)

    assert refused.value.status_code == 400
    assert str(RECENT_REQUESTS_WINDOW) in refused.value.detail
    conn.fetch.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_last_page_of_the_window_stops_at_it():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": RECENT_REQUESTS_WINDOW, "capped": True})
    conn.fetch = AsyncMock(return_value=[])

    page = await _page(conn, offset=RECENT_REQUESTS_WINDOW - 30, limit=200)

    assert page.limit == 30
    *_, limit, offset = conn.fetch.call_args.args
    assert (limit, offset) == (30, RECENT_REQUESTS_WINDOW - 30)


@pytest.mark.asyncio
async def test_a_read_past_its_timeout_is_a_busy_503_not_an_empty_history():
    import asyncpg

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=asyncpg.QueryCanceledError("statement timeout"))

    with pytest.raises(DatabaseBusyError):
        await _page(conn, model_id="zz-no-such-model")
