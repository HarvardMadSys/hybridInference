"""Shared helpers for admin sub-routers.

A helper belongs here only if it is used by two or more sub-routers.
Single-domain helpers live in their owner file. Adding a helper here
that has only one caller is a smell — move it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from serving.schemas_admin import AdminHistogramBucket


def _to_json_safe(value: Any) -> Any:
    """Convert values to JSON-serializable primitives."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _to_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_json_safe(item) for item in value]
    return value


def _serialize_for_audit(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize data dict to JSON-safe primitives for audit logging."""
    return {key: _to_json_safe(value) for key, value in data.items()}


def _build_histogram(
    edges: tuple[float, ...],
    counts_by_bucket: dict[int, int],
) -> list[AdminHistogramBucket]:
    """Map width_bucket() index -> AdminHistogramBucket list with edge bounds.

    width_bucket(x, ARRAY[edges]) returns:
      0  -> x < edges[0]    (underflow; ignored, metrics are non-negative)
      k  -> edges[k-1] <= x < edges[k]   for 1 <= k <= len(edges)-1
      N  -> x >= edges[-1]  (overflow; open-ended upper bound)
    where N = len(edges).

    We always emit one bucket per (edge[k-1], edge[k]) pair plus a final
    open-ended bucket, even if the count is zero, so the UI can render a
    consistent bar chart.
    """
    n_edges = len(edges)
    buckets: list[AdminHistogramBucket] = []
    # Buckets 1..N-1 are bounded.
    for k in range(1, n_edges):
        buckets.append(
            AdminHistogramBucket(
                lower_bound=float(edges[k - 1]),
                upper_bound=float(edges[k]),
                count=int(counts_by_bucket.get(k, 0)),
            )
        )
    # Final overflow bucket (index N): [edges[-1], +inf).
    buckets.append(
        AdminHistogramBucket(
            lower_bound=float(edges[-1]),
            upper_bound=None,
            count=int(counts_by_bucket.get(n_edges, 0)),
        )
    )
    return buckets


def _escape_ilike_substring_term(term: str) -> str:
    """Escape LIKE wildcards so ``ILIKE`` performs literal substring matching."""
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _round_or_none(value: Any, digits: int = 2) -> float | None:
    """Coerce a numeric DB value to float and round, or return None."""
    if value is None:
        return None
    return round(float(value), digits)


def _truncate_hour(dt: datetime) -> datetime:
    """Floor a datetime to the start of its hour (preserving tzinfo)."""
    return dt.replace(minute=0, second=0, microsecond=0)


def _require_aware_utc(dt: datetime, name: str) -> datetime:
    """Reject timezone-naive datetimes; convert tz-aware values to UTC.

    Naive datetimes silently compare to TIMESTAMPTZ using the server/session
    timezone, which produces surprising windows for callers in other zones.
    """
    if dt.tzinfo is None:
        raise HTTPException(
            status_code=400,
            detail=f"`{name}` must be timezone-aware (e.g. ...Z or ...+00:00)",
        )
    return dt.astimezone(timezone.utc)


# --- Recent Requests outcome classes -----------------------------------------
#
# A caller that hangs up mid-stream is logged by
# ``completions_stream._finalize_cancelled`` as status 499 — nginx's "client
# closed request" convention — precisely so the abort is not counted as a
# service fault. The row still carries a non-null ``error``
# ("Client disconnected before the stream completed"), so the historical
# "errors only" predicate sweeps it up alongside real failures and a burst of
# benign disconnects buries the handful of 5xx rows that matter.
#
# The status alone does NOT identify that path. Both failure handlers log
# whatever status they can pull off the upstream exception
# (``completions_stream._extract_exception_status_code``,
# ``routing_info._status_code_from_exception``), so an OpenAI-compatible
# provider that answers 499 — anything behind nginx, including another gateway
# running this software — is logged as an ordinary failure at status 499.
# Classifying every 499 as a local disconnect would take that genuine upstream
# failure out of the error counts and hide it from error triage, which is the
# expensive direction to be wrong in.
#
# So a disconnect is the conjunction: status 499 AND the ``terminal_state`` the
# cancellation path stamps into metadata. The status is kept in the predicate
# even though the terminal state implies it — it narrows on an indexed column
# before the JSONB extraction on a high-volume table.
#
# Rows written before ``terminal_state`` existed (2026-08-27) carry no such key
# and so count as errors, exactly as they did before this split. That is the
# safe fallback: a disconnect left among the errors is the status quo, while a
# real failure moved out of them is a regression.
#
# Note that ``serving.admin.failed_request_alerter.FAILURE_PREDICATE_SQL``
# excludes 499 as a whole class. That is deliberately looser — it decides what
# pages Slack, not what an admin reads — and is left alone here.
CLIENT_DISCONNECT_STATUS_CODE = 499
CLIENT_DISCONNECT_TERMINAL_STATE = "client_disconnect"


def client_disconnect_sql(column_prefix: str = "l.") -> str:
    """Return the SQL predicate matching a gateway-recorded client disconnect.

    ``column_prefix`` is the table alias (with its dot) the caller's query uses,
    or ``""`` for an unaliased one. Only the module's own constants are
    interpolated — nothing here takes caller input.
    """
    return (
        f"({column_prefix}status_code = {CLIENT_DISCONNECT_STATUS_CODE} "
        f"AND {column_prefix}metadata->>'terminal_state' "
        f"= '{CLIENT_DISCONNECT_TERMINAL_STATE}')"
    )


# ``error`` set, or a status outside 2xx/3xx. Unchanged from the predicate the
# ``errors_only`` flag has always applied — client disconnects included.
_OUTCOME_ANY_ERROR_SQL = (
    "(l.error IS NOT NULL OR l.status_code IS NULL OR l.status_code < 200 OR l.status_code >= 400)"
)

# Outcome filters for the Recent Requests list and its JSONL export. Predicates
# are constant (no bind parameters), so a caller can append them to its WHERE
# clauses without disturbing its own placeholder numbering.
#
# ``IS NOT TRUE``, not ``NOT``: the disconnect predicate evaluates to NULL on a
# row with no status or no metadata, and plain ``NOT NULL`` is NULL, which would
# silently drop those rows from ``errors_excluding_disconnects``. A request that
# never got a status back is a failure and must stay in that list.
REQUEST_OUTCOME_FILTERS: dict[str, str] = {
    # No predicate — every row in the window.
    "all": "",
    "errors": _OUTCOME_ANY_ERROR_SQL,
    "errors_excluding_disconnects": (
        f"({_OUTCOME_ANY_ERROR_SQL} AND {client_disconnect_sql()} IS NOT TRUE)"
    ),
    "client_disconnect": client_disconnect_sql(),
}


def resolve_request_outcome_filter(
    outcome: str | None,
    *,
    errors_only: bool = False,
) -> str:
    """Return the SQL predicate for a Recent Requests ``outcome`` filter.

    Returns an empty string when no outcome predicate applies. ``outcome`` wins
    over the older boolean ``errors_only``, which stays supported so existing
    callers (and bookmarked admin URLs) keep working: it is exactly
    ``outcome="errors"``.

    Unlike the ``request_type`` filter, an unrecognized value is rejected rather
    than ignored. Silently dropping a mistyped outcome would answer "show me the
    client disconnects" with the unfiltered stream, and an admin reading that
    page has no way to tell it apart from a window with no disconnects in it.
    """
    if outcome is None or outcome == "":
        return REQUEST_OUTCOME_FILTERS["errors"] if errors_only else ""
    try:
        return REQUEST_OUTCOME_FILTERS[outcome]
    except KeyError:
        raise HTTPException(
            status_code=422,
            detail=(f"`outcome` must be one of: {', '.join(sorted(REQUEST_OUTCOME_FILTERS))}"),
        ) from None
