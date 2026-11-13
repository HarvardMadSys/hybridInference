"""Recent-request queries and presentation for the user dashboard.

``/user/recent-requests`` is one endpoint with two concerns that do not belong
in a router module: a bounded read of ``api_logs`` over the account's newest
:class:`RECENT_REQUESTS_WINDOW` rows, with a bounded in-process COUNT cache,
and the row-to-response mapping that protects auto-model privacy. Both live
here. :mod:`serving.servers.routers.user_routes` keeps the FastAPI
declaration, its dependency injection, and the error handling that turns a
failed read into an empty page.

The window is the point of this module. Whatever a caller asks for reads at
most the account's newest ``RECENT_REQUESTS_WINDOW`` rows: a page lies within
them, the total counts no further, and a filter matches among them only. A
read therefore cannot be made to reach back over an account's whole history,
and it cannot be made to hold a general-pool connection for long -- it runs
under :data:`RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC` with the planner settings
that keep it on the one index range.

The privacy rule is the other part to preserve exactly. When an auto model
served a request, the dashboard shows the model the user asked for -- never the
model that actually served it -- so the row reports ``provider="auto"``,
withholds the routing detail, and replaces the error text with the status's
generic message. Every other row keeps the logged provider, the routing
metadata and the sanitized error.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from serving.exceptions import generic_message_for_status, user_safe_error_for_log
from serving.schemas_auth import RecentRequestItem
from serving.storage.utils import coerce_json_object, escape_ilike_substring_term

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES",
    "RECENT_REQUESTS_COUNT_TTL_SECONDS",
    "RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC",
    "RECENT_REQUESTS_WINDOW",
    "build_user_recent_requests_filters",
    "get_cached_user_request_count",
    "present_recent_requests",
    "query_recent_requests",
]

#: How far back ``/user/recent-requests`` reaches: the account's newest this
#: many requests. A page lies within them, the total counts no further, and a
#: filter matches among them only, so whatever a caller asks for reads at most
#: this many of its rows. The console shows 20 a page: 500 pages.
RECENT_REQUESTS_WINDOW = 10_000

#: How long one of its statements may run, in seconds. Reading the window takes
#: a fraction of that; a statement still running then is stopped and the caller
#: told to retry, rather than holding a general-pool connection for a minute.
RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC = 5.0

# Set for its reads, in their read-only transaction: the statement timeout, and
# the one plan that keeps a read inside the window. The window is the newest
# rows by (timestamp, cost_usd), the order of ``idx_api_logs_user_cost`` read
# backwards within the account; with sorts and bitmap scans off, nothing else
# can produce it. Left to its estimates, the planner walks ``idx_api_logs_timestamp``
# instead for a heavy account -- across every account's rows back to its last
# request, half the table for one that stopped a month ago -- and reads all of
# a light account's rows to sort them.
_RECENT_REQUESTS_READ_SETTINGS = (
    f"SET LOCAL statement_timeout = '{int(RECENT_REQUESTS_STATEMENT_TIMEOUT_SEC * 1000)}ms'; "
    "SET LOCAL enable_sort = off; "
    "SET LOCAL enable_incremental_sort = off; "
    "SET LOCAL enable_bitmapscan = off"
)

#: The window's order, newest first.
_RECENT_ORDER_SQL = "timestamp DESC, cost_usd DESC"

# Bounded in-process TTL cache for the per-user ``api_logs`` row count
# powering ``/user/recent-requests``. The dashboard polls every 60s, so caching
# it per (user, key) keeps the hot path to just the paginated SELECT. A count
# filtered by model is not cached: the model is whatever the caller typed, and
# entries for every string anyone sends would evict everyone else's.
#
# OrderedDict gives us LRU eviction once ``_MAX_ENTRIES`` is reached, which
# bounds memory regardless of how many distinct users hit the endpoint. No
# lock: cache misses for the same key may run a duplicate COUNT under
# concurrent load, which is preferable to serializing all unrelated callers.
RECENT_REQUESTS_COUNT_TTL_SECONDS: float = 60.0
RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES: int = 4096
RECENT_REQUESTS_COUNT_CACHE: OrderedDict[tuple[str, int | None], tuple[float, tuple[int, bool]]] = (
    OrderedDict()
)

#: The model a request's row shows its user: the auto model it asked for, when
#: an auto model served it (serving/auto_mode.py), and the logged model
#: otherwise. A user is never shown which model served an auto request, so the
#: row's provider, routing detail and error text are withheld with it.
_SHOWN_MODEL_SQL = "COALESCE(metadata->'auto'->>'requested', model_id)"

#: The columns a page shows and filters on, read from the window.
_RECENT_ROW_COLUMNS = (
    "request_id, user_id, model_id, provider, timestamp, status_code, latency_ms, "
    "ttft_ms, stream, prompt_tokens, completion_tokens, reasoning_tokens, "
    "cache_read_tokens, cache_write_tokens, total_tokens, cost_usd, error, metadata"
)


def _recent_rows_sql(columns: str) -> str:
    """*columns* of the account's (``$1``) newest :data:`RECENT_REQUESTS_WINDOW` rows, as ``recent``.

    One range of ``idx_api_logs_user_cost`` (``user_id``, ``timestamp``,
    ``cost_usd``), read newest first and never past the window, under
    :data:`_RECENT_REQUESTS_READ_SETTINGS`.
    """
    return (
        f"(SELECT {columns} FROM api_logs WHERE user_id = $1 "
        f"ORDER BY {_RECENT_ORDER_SQL} LIMIT {RECENT_REQUESTS_WINDOW}) AS recent"
    )


def build_user_recent_requests_filters(
    user_id: str, model_id: str | None, api_key_id: int | None = None
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause + bind params for /user/recent-requests.

    Used by both the COUNT cache helper and the paginated SELECT so the two
    queries cannot drift if a future filter is added. ``$1`` is the account;
    the clause filters its window (:func:`_recent_rows_sql`).

    The model filter is a case-insensitive substring of the model the row
    shows, and the key filter reads ``metadata->>'api_key_id'``: the key is not
    a column of its own, so that a log write never depends on a migration of
    ``api_logs``. Neither can use an index, and neither needs one: both read the
    window's rows only, so no filter can make a read reach past it.
    """
    where_clauses = ["user_id = $1"]
    params: list[Any] = [user_id]
    if model_id:
        params.append(escape_ilike_substring_term(model_id))
        where_clauses.append(f"{_SHOWN_MODEL_SQL} ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'")
    if api_key_id is not None:
        params.append(str(api_key_id))
        where_clauses.append(f"metadata->>'api_key_id' = ${len(params)}")
    return " AND ".join(where_clauses), params


async def get_cached_user_request_count(
    conn: Any, user_id: str, model_id: str | None, api_key_id: int | None = None
) -> tuple[int, bool]:
    """Return how many of *user_id*'s window match the filters, and whether there is more.

    The second value is True when the account has requests older than the
    window (:data:`RECENT_REQUESTS_WINDOW`), so the count is a lower bound.
    Both read at most the window: run on *conn* inside the transaction that
    set :data:`_RECENT_REQUESTS_READ_SETTINGS`. Cached for
    :data:`RECENT_REQUESTS_COUNT_TTL_SECONDS`, except a count by model.
    """
    key = (user_id, api_key_id)
    cacheable = not model_id
    now = time.monotonic()
    cached = RECENT_REQUESTS_COUNT_CACHE.get(key) if cacheable else None
    if cached is not None and (now - cached[0]) < RECENT_REQUESTS_COUNT_TTL_SECONDS:
        RECENT_REQUESTS_COUNT_CACHE.move_to_end(key)
        return cached[1]

    where_sql, params = build_user_recent_requests_filters(user_id, model_id, api_key_id)
    # Without a filter on the row's contents, the index alone answers.
    columns = "user_id, model_id, metadata" if len(params) > 1 else "user_id"
    count_row = await conn.fetchrow(
        f"""
        SELECT (SELECT COUNT(*) FROM {_recent_rows_sql(columns)} WHERE {where_sql}) AS total,
               EXISTS (SELECT 1 FROM api_logs WHERE user_id = $1
                       ORDER BY {_RECENT_ORDER_SQL} OFFSET {RECENT_REQUESTS_WINDOW}) AS capped
        """,
        *params,
    )
    total = int(count_row["total"] or 0) if count_row else 0
    counted = (total, bool(count_row and count_row.get("capped")))
    if cacheable:
        RECENT_REQUESTS_COUNT_CACHE[key] = (time.monotonic(), counted)
        RECENT_REQUESTS_COUNT_CACHE.move_to_end(key)
        while len(RECENT_REQUESTS_COUNT_CACHE) > RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES:
            RECENT_REQUESTS_COUNT_CACHE.popitem(last=False)
    return counted


async def query_recent_requests(
    conn: Any,
    *,
    user_id: str,
    model_id: str | None,
    limit: int,
    offset: int,
    api_key_id: int | None = None,
) -> tuple[list[Any], int, bool]:
    """Return one page of the user's logged requests, the total, and whether it is capped.

    The COUNT is cached; the page itself is always a fresh read. Both use the
    same filters, so a page and its total always describe the same result set.
    Both read at most the window: run on *conn* inside the read-only
    transaction that set :data:`_RECENT_REQUESTS_READ_SETTINGS`.
    """
    where_sql, params = build_user_recent_requests_filters(user_id, model_id, api_key_id)
    limit_idx = len(params) + 1
    offset_idx = len(params) + 2
    page_params = [*params, limit, offset]

    total, total_capped = await get_cached_user_request_count(conn, user_id, model_id, api_key_id)
    rows = await conn.fetch(
        f"""
        SELECT
            request_id, {_SHOWN_MODEL_SQL} AS model_id, provider, timestamp,
            status_code, latency_ms, ttft_ms, stream,
            prompt_tokens, completion_tokens, reasoning_tokens,
            cache_read_tokens, cache_write_tokens,
            total_tokens, cost_usd, error,
            metadata->'routewise' AS routewise,
            metadata->>'request_type' AS request_type,
            metadata->'auto'->>'requested' IS NOT NULL AS is_auto,
            metadata->>'api_key_id' AS api_key_id
        FROM {_recent_rows_sql(_RECENT_ROW_COLUMNS)}
        WHERE {where_sql}
        ORDER BY {_RECENT_ORDER_SQL}
        LIMIT ${limit_idx} OFFSET ${offset_idx}
        """,
        *page_params,
    )
    return rows, total, total_capped


def present_recent_requests(
    rows: Iterable[Any], key_prefixes: dict[str, str] | None = None
) -> list[RecentRequestItem]:
    """Map ``api_logs`` rows to the dashboard's response items.

    Auto-model rows are the privacy-sensitive case: they name the model the user
    asked for, report the provider as ``auto``, drop the routing detail and
    replace the logged error with the status's generic message. Everything else
    passes the logged values through, with only the cost coerced for JSON.
    """
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
