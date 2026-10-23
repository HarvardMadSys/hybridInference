"""Recent-request queries and presentation for the user dashboard.

``/user/recent-requests`` is one endpoint with two concerns that do not belong in
a router module: a SQL query over ``api_logs`` with a bounded in-process COUNT
cache, and the row-to-response mapping that protects auto-model privacy. Both
live here. :mod:`serving.servers.routers.user_routes` keeps the FastAPI
declaration, its dependency injection, and the error handling that turns a failed
query into an empty page.

The privacy rule is the part to preserve exactly. When an auto model served a
request, the dashboard shows the model the user asked for -- never the model that
actually served it -- so the row reports ``provider="auto"``, withholds the
routing detail, and replaces the error text with the status's generic message.
Every other row keeps the logged provider, the routing metadata and the
sanitized error.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from serving.exceptions import generic_message_for_status, user_safe_error_for_log
from serving.schemas_auth import RecentRequestItem
from serving.storage.utils import coerce_json_object

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "RECENT_REQUESTS_COUNT_CACHE",
    "RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES",
    "RECENT_REQUESTS_COUNT_TTL_SECONDS",
    "SHOWN_MODEL_SQL",
    "build_user_recent_requests_filters",
    "get_cached_user_request_count",
    "present_recent_requests",
    "query_recent_requests",
]

# Bounded in-process TTL cache for the per-user ``api_logs`` row count powering
# ``/user/recent-requests``. The dashboard polls every 60s and the COUNT(*)
# scales with history size, so caching it per (user, model) keeps the hot path
# to just the paginated SELECT.
#
# OrderedDict gives us LRU eviction once the cap is reached, which bounds memory
# regardless of how many distinct users hit the endpoint. No lock: cache misses
# for the same key may run a duplicate COUNT under concurrent load, which is
# preferable to serializing all unrelated callers.
RECENT_REQUESTS_COUNT_TTL_SECONDS: float = 60.0
RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES: int = 4096
RECENT_REQUESTS_COUNT_CACHE: OrderedDict[tuple[str, str | None], tuple[float, int]] = OrderedDict()

#: The model a request's row shows its user: the auto model it asked for, when
#: an auto model served it (serving/auto_mode.py), and the logged model
#: otherwise. A user is never shown which model served an auto request, so the
#: row's provider, routing detail and error text are withheld with it.
SHOWN_MODEL_SQL = "COALESCE(metadata->'auto'->>'requested', model_id)"


def _escape_ilike_substring_term(term: str) -> str:
    """Escape LIKE wildcards so ``ILIKE`` performs literal substring matching."""
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def build_user_recent_requests_filters(user_id: str, model_id: str | None) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause + bind params for /user/recent-requests.

    Used by both the COUNT cache helper and the paginated SELECT so the two
    queries cannot drift if a future filter is added.
    """
    where_clauses = ["user_id = $1"]
    params: list[Any] = [user_id]
    if model_id:
        params.append(_escape_ilike_substring_term(model_id))
        where_clauses.append(f"{SHOWN_MODEL_SQL} ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'")
    return " AND ".join(where_clauses), params


async def get_cached_user_request_count(conn: Any, user_id: str, model_id: str | None) -> int:
    """Return the cached or freshly-queried ``api_logs`` row count for *user_id*.

    Cached for ``RECENT_REQUESTS_COUNT_TTL_SECONDS`` to keep heavy-history users
    from paying a full COUNT(*) on every 60s dashboard poll.
    """
    key = (user_id, model_id)
    now = time.monotonic()
    cached = RECENT_REQUESTS_COUNT_CACHE.get(key)
    if cached is not None and (now - cached[0]) < RECENT_REQUESTS_COUNT_TTL_SECONDS:
        RECENT_REQUESTS_COUNT_CACHE.move_to_end(key)
        return cached[1]

    where_sql, params = build_user_recent_requests_filters(user_id, model_id)
    count_row = await conn.fetchrow(
        f"""
        SELECT COUNT(*) as total
        FROM api_logs
        WHERE {where_sql}
        """,
        *params,
    )
    total = int(count_row["total"] or 0) if count_row else 0
    RECENT_REQUESTS_COUNT_CACHE[key] = (time.monotonic(), total)
    RECENT_REQUESTS_COUNT_CACHE.move_to_end(key)
    while len(RECENT_REQUESTS_COUNT_CACHE) > RECENT_REQUESTS_COUNT_CACHE_MAX_ENTRIES:
        RECENT_REQUESTS_COUNT_CACHE.popitem(last=False)
    return total


async def query_recent_requests(
    conn: Any,
    *,
    user_id: str,
    model_id: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Any], int]:
    """Return one page of the user's logged requests and the filtered total.

    The COUNT is cached; the page itself is always a fresh read. Both use the
    same filters, so a page and its total always describe the same result set.
    """
    where_sql, params = build_user_recent_requests_filters(user_id, model_id)
    limit_idx = len(params) + 1
    offset_idx = len(params) + 2
    page_params = [*params, limit, offset]

    total = await get_cached_user_request_count(conn, user_id, model_id)
    rows = await conn.fetch(
        f"""
        SELECT
            request_id, {SHOWN_MODEL_SQL} AS model_id, provider, timestamp,
            status_code, latency_ms, ttft_ms, stream,
            prompt_tokens, completion_tokens, reasoning_tokens,
            cache_read_tokens, cache_write_tokens,
            total_tokens, cost_usd, error,
            metadata->'routewise' AS routewise,
            metadata->>'request_type' AS request_type,
            metadata->'auto'->>'requested' IS NOT NULL AS is_auto
        FROM api_logs
        WHERE {where_sql}
        ORDER BY timestamp DESC
        LIMIT ${limit_idx} OFFSET ${offset_idx}
        """,
        *page_params,
    )
    return rows, total


def present_recent_requests(rows: Iterable[Any]) -> list[RecentRequestItem]:
    """Map ``api_logs`` rows to the dashboard's response items.

    Auto-model rows are the privacy-sensitive case: they name the model the user
    asked for, report the provider as ``auto``, drop the routing detail and
    replace the logged error with the status's generic message. Everything else
    passes the logged values through, with only the cost coerced for JSON.
    """
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
        )
        for row in rows
    ]
