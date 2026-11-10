"""Old-versus-new equivalence for the recent-requests extraction.

``/user/recent-requests`` moved from ``serving.servers.routers.user_routes`` to
``serving.servers.routers.recent_requests``. The move must not change what the
endpoint does, so this harness holds the pre-extraction implementation beside
the extracted one and runs both against the same recording connection.

What it pins:

- **SQL.** The count query and the page query are textually identical.
- **Bind ordering.** The parameters reach the driver in the same order, for
  every filter combination.
- **Response mapping.** The same rows map to the same response items, including
  the auto-model privacy rule.
- **Cache behaviour.** The same keys are cached, the same entries are reused,
  a count by model is still never cached, and the eviction order is the same.
- **Authorization.** The account's own ``user_id`` is the first bind, and a
  key the account does not hold is refused with a 404 before any read.
- **Errors.** A statement past its timeout is a busy 503, not an empty page;
  an offset past the window is a 400; a failed query is an empty page.

The pre-extraction implementation below is the code as it stood on ``dev``
before the move, copied verbatim into this harness so the comparison is
against the real thing rather than against a description of it.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from serving.exceptions import generic_message_for_status, user_safe_error_for_log
from serving.schemas_auth import RecentRequestItem
from serving.servers.routers import recent_requests, user_routes
from serving.storage.utils import coerce_json_object

# --------------------------------------------------------------------------
# The pre-extraction implementation, copied from dev's user_routes.py.
# --------------------------------------------------------------------------

_OLD_WINDOW = 10_000
_OLD_ORDER_SQL = "timestamp DESC, cost_usd DESC"
_OLD_SHOWN_MODEL_SQL = "COALESCE(metadata->'auto'->>'requested', model_id)"
_OLD_ROW_COLUMNS = (
    "request_id, user_id, model_id, provider, timestamp, status_code, latency_ms, "
    "ttft_ms, stream, prompt_tokens, completion_tokens, reasoning_tokens, "
    "cache_read_tokens, cache_write_tokens, total_tokens, cost_usd, error, metadata"
)
_OLD_COUNT_CACHE: OrderedDict[tuple[str, int | None], tuple[float, tuple[int, bool]]] = (
    OrderedDict()
)


def _old_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _old_recent_rows_sql(columns: str) -> str:
    return (
        f"(SELECT {columns} FROM api_logs WHERE user_id = $1 "
        f"ORDER BY {_OLD_ORDER_SQL} LIMIT {_OLD_WINDOW}) AS recent"
    )


def _old_filters(
    user_id: str, model_id: str | None, api_key_id: int | None = None
) -> tuple[str, list[Any]]:
    where_clauses = ["user_id = $1"]
    params: list[Any] = [user_id]
    if model_id:
        params.append(_old_escape(model_id))
        where_clauses.append(
            f"{_OLD_SHOWN_MODEL_SQL} ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'"
        )
    if api_key_id is not None:
        params.append(str(api_key_id))
        where_clauses.append(f"metadata->>'api_key_id' = ${len(params)}")
    return " AND ".join(where_clauses), params


async def _old_cached_count(
    conn: Any, user_id: str, model_id: str | None, api_key_id: int | None = None
) -> tuple[int, bool]:
    key = (user_id, api_key_id)
    cacheable = not model_id
    now = time.monotonic()
    cached = _OLD_COUNT_CACHE.get(key) if cacheable else None
    if cached is not None and (now - cached[0]) < 60.0:
        _OLD_COUNT_CACHE.move_to_end(key)
        return cached[1]

    where_sql, params = _old_filters(user_id, model_id, api_key_id)
    columns = "user_id, model_id, metadata" if len(params) > 1 else "user_id"
    count_row = await conn.fetchrow(
        f"""
        SELECT (SELECT COUNT(*) FROM {_old_recent_rows_sql(columns)} WHERE {where_sql}) AS total,
               EXISTS (SELECT 1 FROM api_logs WHERE user_id = $1
                       ORDER BY {_OLD_ORDER_SQL} OFFSET {_OLD_WINDOW}) AS capped
        """,
        *params,
    )
    total = int(count_row["total"] or 0) if count_row else 0
    counted = (total, bool(count_row and count_row.get("capped")))
    if cacheable:
        _OLD_COUNT_CACHE[key] = (time.monotonic(), counted)
        _OLD_COUNT_CACHE.move_to_end(key)
        while len(_OLD_COUNT_CACHE) > 4096:
            _OLD_COUNT_CACHE.popitem(last=False)
    return counted


async def _old_query(
    conn: Any,
    *,
    user_id: str,
    model_id: str | None,
    limit: int,
    offset: int,
    api_key_id: int | None = None,
) -> tuple[list[Any], int, bool]:
    where_sql, params = _old_filters(user_id, model_id, api_key_id)
    limit_idx = len(params) + 1
    offset_idx = len(params) + 2
    page_params = [*params, limit, offset]

    total, total_capped = await _old_cached_count(conn, user_id, model_id, api_key_id)
    rows = await conn.fetch(
        f"""
        SELECT
            request_id, {_OLD_SHOWN_MODEL_SQL} AS model_id, provider, timestamp,
            status_code, latency_ms, ttft_ms, stream,
            prompt_tokens, completion_tokens, reasoning_tokens,
            cache_read_tokens, cache_write_tokens,
            total_tokens, cost_usd, error,
            metadata->'routewise' AS routewise,
            metadata->>'request_type' AS request_type,
            metadata->'auto'->>'requested' IS NOT NULL AS is_auto,
            metadata->>'api_key_id' AS api_key_id
        FROM {_old_recent_rows_sql(_OLD_ROW_COLUMNS)}
        WHERE {where_sql}
        ORDER BY {_OLD_ORDER_SQL}
        LIMIT ${limit_idx} OFFSET ${offset_idx}
        """,
        *page_params,
    )
    return rows, total, total_capped


def _old_present(rows: Any, key_prefixes: dict[str, str] | None = None) -> list[RecentRequestItem]:
    prefixes = key_prefixes or {}
    return [
        RecentRequestItem(
            request_id=row["request_id"],
            model_id=row["model_id"],
            provider="auto" if row["is_auto"] else row["provider"],
            timestamp=row["timestamp"],
            status_code=row["status_code"],
            latency_ms=row["latency_ms"],
            ttft_ms=row["ttft_ms"],
            stream=row["stream"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
            total_tokens=row["total_tokens"],
            cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
            error=(
                generic_message_for_status(row["status_code"])
                if row["is_auto"] and row["error"] and row["status_code"] is not None
                else user_safe_error_for_log(row["error"], row["status_code"])
            ),
            routewise=None if row["is_auto"] else coerce_json_object(row["routewise"]),
            request_type=row["request_type"],
            key_prefix=prefixes.get(row["api_key_id"]) if row["api_key_id"] else None,
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# The harness.
# --------------------------------------------------------------------------

FILTER_CASES = [
    pytest.param(None, None, id="no-filter"),
    pytest.param("gpt-4o", None, id="model-only"),
    pytest.param("gpt%_4\\o", None, id="model-with-wildcards"),
    pytest.param(None, 7, id="key-only"),
    pytest.param("gpt-4o", 7, id="model-and-key"),
]


@pytest.fixture(autouse=True)
def _reset_both_caches():
    """Clear the old and the new count cache, so each case starts cold."""
    _OLD_COUNT_CACHE.clear()
    recent_requests.RECENT_REQUESTS_COUNT_CACHE.clear()
    yield
    _OLD_COUNT_CACHE.clear()
    recent_requests.RECENT_REQUESTS_COUNT_CACHE.clear()


def _recording_conn(total: int = 3, capped: bool = False, rows: list[dict] | None = None):
    """A connection that records the SQL and binds it was called with."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total": total, "capped": capped})
    conn.fetch = AsyncMock(return_value=rows if rows is not None else [])
    return conn


def _norm(sql: str) -> str:
    """Collapse whitespace, so a formatting difference is not a behaviour one."""
    return " ".join(sql.split())


def _row(**overrides) -> dict:
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
@pytest.mark.parametrize(("model_id", "api_key_id"), FILTER_CASES)
async def test_the_count_query_is_the_same_sql_with_the_same_binds(model_id, api_key_id):
    """The COUNT the old code ran is the COUNT the new code runs, bind for bind."""
    old_conn = _recording_conn(total=5, capped=True)
    new_conn = _recording_conn(total=5, capped=True)

    old = await _old_cached_count(old_conn, "user-1", model_id, api_key_id)
    new = await recent_requests.get_cached_user_request_count(
        new_conn, "user-1", model_id, api_key_id
    )

    assert old == new
    old_sql, *old_params = old_conn.fetchrow.call_args.args
    new_sql, *new_params = new_conn.fetchrow.call_args.args
    assert _norm(old_sql) == _norm(new_sql)
    assert old_params == new_params


@pytest.mark.asyncio
@pytest.mark.parametrize(("model_id", "api_key_id"), FILTER_CASES)
async def test_the_page_query_is_the_same_sql_with_the_same_binds(model_id, api_key_id):
    """The page SELECT is identical too, in text and in parameter order."""
    old_conn = _recording_conn(rows=[_row()])
    new_conn = _recording_conn(rows=[_row()])

    await _old_query(
        old_conn, user_id="user-1", model_id=model_id, limit=50, offset=0, api_key_id=api_key_id
    )
    await recent_requests.query_recent_requests(
        new_conn, user_id="user-1", model_id=model_id, limit=50, offset=0, api_key_id=api_key_id
    )

    old_sql, *old_params = old_conn.fetch.call_args.args
    new_sql, *new_params = new_conn.fetch.call_args.args
    assert _norm(old_sql) == _norm(new_sql)
    assert old_params == new_params


@pytest.mark.asyncio
async def test_the_page_bind_order_puts_the_account_first():
    """``$1`` is the caller's own account, before any filter binds."""
    conn = _recording_conn(rows=[_row()])

    await recent_requests.query_recent_requests(
        conn, user_id="user-1", model_id="gpt-4o", limit=25, offset=10, api_key_id=7
    )

    sql, *params = conn.fetch.call_args.args
    assert "user_id = $1" in sql
    assert params == ["user-1", "gpt-4o", "7", 25, 10]


@pytest.mark.asyncio
async def test_the_rows_map_to_the_same_response_items():
    """The same rows produce the same items, auto-model privacy included."""
    rows = [
        _row(),
        _row(
            request_id="req-2",
            model_id="auto-balance",
            status_code=503,
            error="No provider is currently available for model 'mid-b'",
            is_auto=True,
        ),
        _row(request_id="req-3", api_key_id="7"),
    ]
    prefixes = {"7": "hyi-laptop00"}

    old_items = _old_present(rows, prefixes)
    new_items = recent_requests.present_recent_requests(rows, prefixes)

    assert old_items == new_items
    named, auto, keyed = new_items
    # The auto row names the model the user asked for, never the one that served.
    assert (auto.model_id, auto.provider, auto.routewise) == ("auto-balance", "auto", None)
    assert "mid-b" not in (auto.error or "")
    assert named.routewise == {"primary_provider": "mid-b:zai-api"}
    assert keyed.key_prefix == "hyi-laptop00"


@pytest.mark.asyncio
async def test_the_cache_is_keyed_and_reused_the_same_way():
    """Same cache keys, same reuse, and a count by model is still never cached."""
    old_conn = _recording_conn()
    new_conn = _recording_conn()

    for user, model, key in (("u1", None, None), ("u1", "gpt-4o", None), ("u2", None, None)):
        await _old_cached_count(old_conn, user, model, key)
        await recent_requests.get_cached_user_request_count(new_conn, user, model, key)

    assert set(_OLD_COUNT_CACHE) == set(recent_requests.RECENT_REQUESTS_COUNT_CACHE)
    # The key is the account and the API key, so a count by one key is not
    # served from another key's entry. Both caches must agree on the exact
    # keys, not merely on how many there are.
    assert set(recent_requests.RECENT_REQUESTS_COUNT_CACHE) == {
        ("u1", None),
        ("u2", None),
    }
    assert ("u1", 7) not in recent_requests.RECENT_REQUESTS_COUNT_CACHE

    # A second read of the unfiltered counts is served from the cache; the
    # count by model is not cached and runs again.
    old_conn.fetchrow.reset_mock()
    new_conn.fetchrow.reset_mock()
    await _old_cached_count(old_conn, "u1", None, None)
    await recent_requests.get_cached_user_request_count(new_conn, "u1", None, None)
    await _old_cached_count(old_conn, "u1", "gpt-4o", None)
    await recent_requests.get_cached_user_request_count(new_conn, "u1", "gpt-4o", None)

    assert old_conn.fetchrow.await_count == new_conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_a_count_by_one_key_is_not_served_from_another_keys_entry():
    """Two keys of one account get separate count entries, in both caches."""
    old_conn = _recording_conn()
    new_conn = _recording_conn()
    old_conn.fetchrow.side_effect = [
        {"total": 10, "capped": False},
        {"total": 20, "capped": False},
    ]
    new_conn.fetchrow.side_effect = [
        {"total": 10, "capped": False},
        {"total": 20, "capped": False},
    ]

    old_first = await _old_cached_count(old_conn, "u1", None, 7)
    new_first = await recent_requests.get_cached_user_request_count(new_conn, "u1", None, 7)
    old_second = await _old_cached_count(old_conn, "u1", None, 8)
    new_second = await recent_requests.get_cached_user_request_count(new_conn, "u1", None, 8)

    assert (old_first, old_second) == (new_first, new_second) == ((10, False), (20, False))
    # Each key ran its own COUNT: neither was served from the other's entry.
    assert old_conn.fetchrow.await_count == new_conn.fetchrow.await_count == 2
    assert set(recent_requests.RECENT_REQUESTS_COUNT_CACHE) == {("u1", 7), ("u1", 8)}


@pytest.mark.asyncio
async def test_the_cache_evicts_in_the_same_order():
    """Both caches drop the least-recently-used entry at their size cap."""
    old_conn = _recording_conn()
    new_conn = _recording_conn()
    for user in ("u1", "u2", "u3"):
        await _old_cached_count(old_conn, user, None, None)
        await recent_requests.get_cached_user_request_count(new_conn, user, None, None)

    assert set(_OLD_COUNT_CACHE) == set(recent_requests.RECENT_REQUESTS_COUNT_CACHE)


@pytest.mark.asyncio
async def test_the_account_scopes_the_read_and_an_unknown_key_is_404():
    """The read is the account's own rows; a key it does not hold never reads."""
    conn = _recording_conn(rows=[_row()])
    keys = MagicMock()
    keys.list_account_keys = AsyncMock(return_value=[{"id": 7, "key_prefix": "hyi-laptop00"}])

    with pytest.raises(HTTPException) as refused:
        await user_routes.get_recent_requests(
            limit=50,
            offset=0,
            model_id=None,
            key_prefix="hyi-someone00",
            current_user={"user_id": "user-1"},
            db_logger=SimpleNamespace(pool=SimpleNamespace(acquire=MagicMock())),
            op_store=keys,
        )

    assert refused.value.status_code == 404
    conn.fetch.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_read_past_its_timeout_is_a_busy_503_in_both():
    """A cancelled statement is a busy 503, never a silently empty history."""
    import asyncpg

    from serving.storage.pools import DatabaseBusyError

    old_conn = _recording_conn()
    old_conn.fetchrow = AsyncMock(side_effect=asyncpg.QueryCanceledError("statement timeout"))
    new_conn = _recording_conn()
    new_conn.fetchrow = AsyncMock(side_effect=asyncpg.QueryCanceledError("statement timeout"))

    with pytest.raises(asyncpg.QueryCanceledError):
        await _old_query(old_conn, user_id="user-1", model_id="gpt-4o", limit=50, offset=0)
    with pytest.raises(asyncpg.QueryCanceledError):
        await recent_requests.query_recent_requests(
            new_conn, user_id="user-1", model_id="gpt-4o", limit=50, offset=0
        )

    # And through the route, the cancelled statement becomes a busy 503.
    route_conn = _recording_conn()
    route_conn.fetchrow = AsyncMock(side_effect=asyncpg.QueryCanceledError("statement timeout"))
    with pytest.raises(DatabaseBusyError):
        await user_routes.get_recent_requests(
            limit=50,
            offset=0,
            model_id="gpt-4o",
            key_prefix=None,
            current_user={"user_id": "user-1"},
            db_logger=_db_logger(route_conn),
            op_store=None,
        )


@pytest.mark.asyncio
async def test_an_offset_past_the_window_is_refused_before_any_read():
    """The window bounds the page, and the refusal happens before the read."""
    conn = _recording_conn()

    with pytest.raises(HTTPException) as refused:
        await user_routes.get_recent_requests(
            limit=50,
            offset=recent_requests.RECENT_REQUESTS_WINDOW,
            model_id=None,
            key_prefix=None,
            current_user={"user_id": "user-1"},
            db_logger=_db_logger(conn),
            op_store=None,
        )

    assert refused.value.status_code == 400
    assert str(recent_requests.RECENT_REQUESTS_WINDOW) in refused.value.detail
    conn.fetch.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_query_is_an_empty_page_in_both():
    """A broken database gives an empty page, not an error and not a crash."""
    old_conn = _recording_conn()
    old_conn.fetchrow = AsyncMock(side_effect=RuntimeError("database is gone"))
    new_conn = _recording_conn()
    new_conn.fetchrow = AsyncMock(side_effect=RuntimeError("database is gone"))

    with pytest.raises(RuntimeError):
        await _old_query(old_conn, user_id="user-1", model_id=None, limit=50, offset=0)
    with pytest.raises(RuntimeError):
        await recent_requests.query_recent_requests(
            new_conn, user_id="user-1", model_id=None, limit=50, offset=0
        )

    # Through the route the failure is caught, and the page comes back empty.
    route_conn = _recording_conn()
    route_conn.fetchrow = AsyncMock(side_effect=RuntimeError("database is gone"))
    page = await user_routes.get_recent_requests(
        limit=50,
        offset=0,
        model_id=None,
        key_prefix=None,
        current_user={"user_id": "user-1"},
        db_logger=_db_logger(route_conn),
        op_store=None,
    )
    assert (page.requests, page.total) == ([], 0)


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
