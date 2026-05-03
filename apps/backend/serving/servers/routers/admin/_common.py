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
