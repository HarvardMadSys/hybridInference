"""Attempt history and routing metadata for the hybrid dispatch layer.

One request handled by :class:`routing.hybrid.HybridRouter` can be served by
more than one execution backend: attempt 1 on local, the answer from cloud. The
``_routing`` metadata that explains that -- which attempts failed, which backend
answered, whether the policy's preferred target could be honored -- is plain
data, and the streaming path serializes the same records into one synthetic SSE
frame.

That record shape lives here so it has a single home: the router keeps the
coordination (which attempt runs next, which error a caller sees, when a stream
is committed) and this module only materializes the metadata those decisions
produce. Field names are part of the contract: the DB log and the fallback
diagnostic resolve an attempt's provider from these records, and a record that
drops a field loses attribution instead of failing loudly.

The single-router equivalents live in :mod:`routing.telemetry`, which builds
them from an adapter it holds. This layer holds no adapters, so an attempt's
provider and endpoint can only come from the failing router's own ``_routing``
block, and the record is keyed by the backend that ran it.

Nothing here selects a backend, dispatches an attempt, or decides an outcome.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from routing.decisions import RoutingTarget

__all__ = [
    "attach_attempt_history",
    "attempt_history_chunk",
    "attempt_record",
    "merge_attempt_history",
    "tag_backend",
    "tag_preference",
]


def attempt_record(
    backend_name: str,
    endpoint_id: str | None,
    exc: BaseException,
) -> dict[str, Any]:
    """Return the routing-metadata record for one failed attempt.

    The shape keeps the fields the single router's ``failed_attempt`` produced --
    ``provider``, ``endpoint_id``, ``error_type``, ``error`` -- because consumers
    read them: the DB log and the fallback diagnostic both resolve an attempt's
    provider from here, and a record without one loses the attribution rather
    than failing loudly. ``backend`` is added, since once one request can span two
    execution domains "which domain" is worth recording and no legacy field
    carries it.

    ``provider`` comes from the failing router's own ``_routing`` block, which is
    the only place this layer can read it: it holds no adapters. An attempt that
    failed before any router touched it -- an empty stream -- has no provider to
    report, and the caller falls back to the endpoint id as it always did.
    """
    error_routing = getattr(exc, "_routing", None)
    routing = error_routing if isinstance(error_routing, dict) else {}
    resolved = routing.get("endpoint_id") or endpoint_id or backend_name
    record: dict[str, Any] = {
        "endpoint_id": str(resolved),
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }
    provider = routing.get("provider")
    if provider:
        record["provider"] = str(provider)
    record["backend"] = backend_name
    return record


def merge_attempt_history(response: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
    """Merge every earlier attempt into the answer's routing metadata."""
    if not attempts:
        return
    routing = response.get("_routing")
    if not isinstance(routing, dict):
        routing = {}
        response["_routing"] = routing
    routing.setdefault("fallback", True)
    existing = routing.get("failed_attempts")
    routing["failed_attempts"] = [
        *(existing if isinstance(existing, list) else []),
        *attempts,
    ]


def attempt_history_chunk(
    attempts: list[dict[str, Any]],
    *,
    target: RoutingTarget | None,
    resolved_endpoint: str | None,
) -> str:
    """Build the routing-metadata frame carrying earlier backends' attempts.

    Also carries the preferred target, so the stream's routing metadata explains
    why this fallback happened the same way the non-streaming answer does.
    """
    routing: dict[str, Any] = {"fallback": True, "failed_attempts": attempts}
    if resolved_endpoint is not None:
        routing["preferred_endpoint_id"] = resolved_endpoint
    if target is not None:
        routing["preferred_target"] = target.describe()
        routing["preference_in_range"] = resolved_endpoint is not None
    payload = {"choices": [], "_routing": routing}
    return f"data: {json.dumps(payload)}\n\n"


def attach_attempt_history(exc: BaseException, attempts: list[dict[str, Any]]) -> None:
    """Record every attempt on the error the caller is told about.

    The error may already carry the attempt its own router made, so the records
    are merged by endpoint: the router saw one candidate, this layer saw them all,
    and the caller should see each endpoint exactly once.
    """
    if not attempts:
        return
    routing = getattr(exc, "_routing", None)
    if not isinstance(routing, dict):
        return
    existing = routing.get("failed_attempts")
    if not isinstance(existing, list):
        routing["failed_attempts"] = list(attempts)
        return
    known = {record.get("endpoint_id") for record in existing if isinstance(record, dict)}
    for record in attempts:
        endpoint_id = record.get("endpoint_id")
        if endpoint_id in known:
            continue
        existing.append(record)
        known.add(endpoint_id)


def tag_backend(response: dict[str, Any], backend_name: str) -> None:
    """Record the serving backend in a response's routing metadata."""
    routing = response.get("_routing")
    if isinstance(routing, dict):
        routing.setdefault("backend", backend_name)


def tag_preference(
    response: dict[str, Any],
    target: RoutingTarget | None,
    resolved_endpoint: str | None,
) -> None:
    """Record the policy's target and whether this backend could honor it."""
    routing = response.get("_routing")
    if not isinstance(routing, dict):
        return
    if resolved_endpoint is not None:
        routing.setdefault("preferred_endpoint_id", resolved_endpoint)
    if target is not None:
        routing.setdefault("preferred_target", target.describe())
        routing.setdefault("preference_in_range", resolved_endpoint is not None)
