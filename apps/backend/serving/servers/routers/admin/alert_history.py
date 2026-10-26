"""Read-only alert history for administrators and scoped reader tokens.

Every answer reads committed PostgreSQL rows only, and every answer says how
far that goes: completeness is ``best_effort``, unknown gaps are always
possible, and late arrivals can change a page already read. A database that
cannot be read is reported as unavailable, never as an empty history.

The in-memory buffer shown under ``this_process`` belongs to the worker that
happens to answer; other workers' buffers are known only through the last
snapshot they managed to write, shown with its report time.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from serving.observability.alert_history.records import utc_iso
from serving.observability.alert_history.runtime import HistoryUnavailable, get_runtime
from serving.servers.routers.alert_history_auth import (
    DEFAULT_STALE_AFTER_SEC,
    CredentialsUnavailable,
    ReaderIdentity,
    load_credentials,
    require_history_reader,
)
from serving.storage import alert_history_store as store
from serving.utils.logging import get_logger

router = APIRouter(prefix="/admin/alert-history", tags=["Admin"])
logger = get_logger(__name__)

#: What a page of events is, said in every paged answer.
PAGINATION_NOTE = (
    "keyset on recorded_at (insert-time database clock), not commit order and "
    "not a snapshot: a record whose transaction commits after a cursor passed "
    "its recorded_at is not on later pages. To collect everything, rescan the "
    "occurred_at window from the start and deduplicate by record_id; no finite "
    "overlap guarantees completeness for arbitrarily delayed records."
)
MAX_WINDOW = dt.timedelta(days=7)
DEFAULT_WINDOW = dt.timedelta(hours=24)
_FILTER = re.compile(r"^[A-Za-z0-9._:@/ -]{1,200}$")
_REQUEST_ID = re.compile(r"^[\x21-\x7e]{1,128}$")


def _error(status_code: int, error_type: str, message: str) -> JSONResponse:
    headers = {"Retry-After": "5"} if status_code in (429, 503) else {}
    return JSONResponse(
        {"error": {"type": error_type, "message": message}},
        status_code=status_code,
        headers=headers,
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return utc_iso(value)
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(v) for v in value]
    return value


def _envelope(now: dt.datetime, **body: Any) -> dict[str, Any]:
    return {
        "query_time": utc_iso(now),
        "completeness": "best_effort",
        "unknown_gaps_possible": True,
        "late_arrivals_possible": True,
        **body,
    }


def _window(
    since: dt.datetime | None, until: dt.datetime | None, now: dt.datetime
) -> tuple[dt.datetime, dt.datetime] | JSONResponse:
    for value in (since, until):
        if value is not None and value.tzinfo is None:
            return _error(422, "invalid_request", "Timestamps need an explicit offset.")
    end = until or now
    start = since or (end - DEFAULT_WINDOW)
    return _checked_window(start, end)


def _checked_window(
    start: dt.datetime, end: dt.datetime
) -> tuple[dt.datetime, dt.datetime] | JSONResponse:
    """Order and span rules every window obeys, resolved here or carried in a cursor."""
    if start.tzinfo is None or end.tzinfo is None:
        return _error(422, "invalid_request", "Timestamps need an explicit offset.")
    if start >= end:
        return _error(422, "invalid_request", "since must be before until.")
    if end - start > MAX_WINDOW:
        return _error(
            422, "invalid_request", "A query spans at most 7 days; query older history in windows."
        )
    return start, end


def _carried_window(
    carried: tuple[dt.datetime, dt.datetime],
    since: dt.datetime | None,
    until: dt.datetime | None,
) -> tuple[dt.datetime, dt.datetime] | JSONResponse:
    """A cursor's window, held to the rules of the first page that minted it.

    The cursor is not signed, so its window is only a claim: it must pass the
    same checks as a resolved window and agree with whatever bound the query
    named explicitly (the fingerprint pins those, not the window).
    """
    window = _checked_window(*carried)
    if isinstance(window, JSONResponse):
        return window
    if (since is not None and window[0] != since) or (until is not None and window[1] != until):
        return _error(422, "invalid_request", "The cursor does not belong to this query.")
    return window


def _fingerprint(identity: ReaderIdentity, **parts: Any) -> str:
    material = json.dumps(
        {"who": identity.principal, **{k: _json_ready(v) for k, v in parts.items()}},
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _runtime_or_error() -> Any:
    runtime = get_runtime()
    if runtime is None or not runtime.has_database:
        return _error(
            404, "alert_history_not_configured", "This deployment keeps no alert history."
        )
    return runtime


def _unavailable(exc: BaseException) -> JSONResponse:
    code = exc.code if isinstance(exc, HistoryUnavailable) else "database_unavailable"
    if not isinstance(exc, HistoryUnavailable):
        logger.warning("alert history read failed: %s", type(exc).__name__)
    return _error(503, "history_unavailable", f"History cannot be read right now ({code}).")


def _page(
    rows: list[dict[str, Any]],
    limit: int,
    key: str,
    fingerprint: str,
    window: tuple[dt.datetime, dt.datetime] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = None
    if more and rows:
        last = rows[-1]
        cursor = store.encode_cursor((last[key], last["record_id"]), fingerprint, window)
    return rows, cursor


async def _coverage_for(
    conn: Any, identity: ReaderIdentity, pairs: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    cov = await store.coverage(conn, scope=identity.scope, pairs=sorted(pairs))
    return [
        {
            "source": source,
            "source_namespace": namespace,
            "first_recorded_at": cov.get((source, namespace), {}).get("first_recorded_at"),
            "last_recorded_at": cov.get((source, namespace), {}).get("last_recorded_at"),
        }
        for source, namespace in sorted(pairs)
    ]


# -- status -------------------------------------------------------------------

#: Most target rows one status answer lists; past it ``targets_truncated``.
MAX_TARGETS = 200
#: Most recent instances listed per target; past it ``instances_truncated``.
INSTANCES_PER_TARGET = 20
#: Known gaps listed; past it ``known_gaps_truncated`` and use the events read.
MAX_GAPS = 50


def _configured_targets(
    identity: ReaderIdentity, runtime: Any
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], str]:
    """Every ``(source, namespace, environment)`` configuration expects to report."""
    targets: dict[tuple[str, str, str], dict[str, Any]] = {}
    ident = runtime.identity
    if identity.scope.allows(
        deployment_id=ident.deployment_id,
        target_environment=ident.target_environment,
        source="backend",
    ):
        targets[("backend", ident.source_namespace, ident.target_environment)] = {
            "deployment_id": ident.deployment_id,
            "configured": True,
            "capture_enabled": runtime.enabled,
            "stale_after_sec": DEFAULT_STALE_AFTER_SEC,
        }
    try:
        writers = load_credentials().writers
        credential_state = "ok"
    except CredentialsUnavailable as exc:
        writers = ()
        credential_state = f"unavailable:{exc}"
    for writer in writers:
        for env in writer.target_environments:
            if not identity.scope.allows(
                deployment_id=writer.deployment_id, target_environment=env, source=writer.source
            ):
                continue
            entry = targets.setdefault(
                (writer.source, writer.source_namespace, env),
                {
                    "deployment_id": writer.deployment_id,
                    "configured": True,
                    "capture_enabled": runtime.enabled,
                    "stale_after_sec": writer.stale_after_sec,
                },
            )
            # Two credentials during a rotation: the more patient cadence wins.
            entry["stale_after_sec"] = max(entry["stale_after_sec"], writer.stale_after_sec)
    return targets, credential_state


def _age(now: dt.datetime, moment: dt.datetime | None) -> float | None:
    return None if moment is None else (now - moment).total_seconds()


def _instance_state(row: dict[str, Any], now: dt.datetime, stale_after: float) -> str:
    if row.get("stopped_at"):
        # A shutdown or a completed invocation said so itself.
        return "stopped"
    age = _age(now, row.get("last_report_at"))
    if age is None or age > stale_after:
        # No closing report and no recent one: it may have crashed. Never
        # read silence as a clean stop.
        return "unknown"
    return "running"


def _instance(row: dict[str, Any], now: dt.datetime, stale_after: float) -> dict[str, Any]:
    return {
        "source_instance_id": row["source_instance_id"],
        "state": _instance_state(row, now, stale_after),
        "release_id": row["release_id"],
        "started_at": row["started_at"],
        "last_report_at": row["last_report_at"],
        "last_write_at": row["last_write_at"],
        "sync_watermark": row["sync_watermark"],
        "pending_snapshot": {
            "count": row["pending_count"],
            "bytes": row["pending_bytes"],
            "oldest_pending_at": row["oldest_pending_at"],
            "reported_at": row["pending_reported_at"],
        },
        "dropped_count": row["dropped_count"],
        "refused_count": row["refused_count"],
        "last_error_code": row["last_error_code"],
        "last_error_at": row["last_error_at"],
        "stopped_at": row["stopped_at"],
        "stop_reason": row["stop_reason"],
        "lost_at_stop": row["lost_at_stop"],
    }


def _connection(entry: dict[str, Any], state: dict[str, Any], now: dt.datetime) -> str:
    if entry["configured"] and entry["capture_enabled"] is False:
        return "disabled"
    if not state["instance_count"]:
        # Exact for this target: looked up on its own, not inferred from a list.
        return "never_reported"
    age = _age(now, state["last_report_at"])
    if age is not None and age <= entry["stale_after_sec"]:
        return "reporting"
    return "stale"


@router.get("/status")
async def history_status(
    identity: ReaderIdentity = Depends(require_history_reader),
) -> JSONResponse:
    """Per source and target environment: connection, coverage, writes, gaps."""
    now = dt.datetime.now(dt.UTC)
    runtime = get_runtime()
    if runtime is None or not runtime.has_database:
        return JSONResponse(
            _envelope(
                now,
                database={"status": "not_configured"},
                capture={"enabled": False},
                sources=[],
                known_gaps=[],
            )
        )
    configured, credential_state = _configured_targets(identity, runtime)
    this_process = runtime.local_status() if identity.kind == "admin" else None
    capture = {"enabled": runtime.enabled, "this_process": this_process}
    if runtime.read_slots.locked():
        return _error(429, "too_many_requests", "Too many concurrent history reads.")
    targets = dict(configured)
    try:
        async with runtime.read_slots, runtime.acquire() as conn:
            observed, observed_truncated = await store.observed_targets(
                conn, scope=identity.scope, limit=MAX_TARGETS
            )
            for source, namespace, env, deployment in observed:
                targets.setdefault(
                    (source, namespace, env),
                    {
                        "deployment_id": deployment,
                        "configured": False,
                        "capture_enabled": None,
                        "stale_after_sec": DEFAULT_STALE_AFTER_SEC,
                    },
                )
            # Configured targets first: they are what "never reported" is about.
            ordered = sorted(targets, key=lambda t: (not targets[t]["configured"], t))
            shown = ordered[:MAX_TARGETS]
            states = await store.target_status(
                conn, scope=identity.scope, targets=shown, instance_limit=INSTANCES_PER_TARGET
            )
            gaps = await store.known_gaps(
                conn, scope=identity.scope, since=now - MAX_WINDOW, until=now, limit=MAX_GAPS + 1
            )
    except Exception as exc:
        # The database is the only record; without it nothing can be claimed,
        # not even that a configured source never reported.
        return JSONResponse(
            _json_ready(
                _envelope(
                    now,
                    database={
                        "status": "unavailable",
                        "error_code": exc.code
                        if isinstance(exc, HistoryUnavailable)
                        else "database_unavailable",
                    },
                    capture=capture,
                    credentials=credential_state,
                    sources=[
                        {
                            "source": key[0],
                            "source_namespace": key[1],
                            "target_environment": key[2],
                            **entry,
                            "connection": "unknown",
                            "instances": [],
                        }
                        for key, entry in sorted(configured.items())
                    ],
                    known_gaps=None,
                )
            )
        )
    sources = []
    for key in shown:
        entry = targets[key]
        state = states[key]
        rows = state["instances"]
        stale_after = float(entry["stale_after_sec"])
        sources.append(
            {
                "source": key[0],
                "source_namespace": key[1],
                "target_environment": key[2],
                **entry,
                "connection": _connection(entry, state, now),
                "first_recorded_at": state["first_recorded_at"],
                "last_recorded_at": state["last_recorded_at"],
                "last_successful_write_at": state["last_write_at"],
                "last_report_at": state["last_report_at"],
                "instance_count": state["instance_count"],
                "instance_count_is_floor": state["instance_count_is_floor"],
                "instances": [_instance(r, now, stale_after) for r in rows[:INSTANCES_PER_TARGET]],
                "instances_truncated": len(rows) > INSTANCES_PER_TARGET,
            }
        )
    return JSONResponse(
        _json_ready(
            _envelope(
                now,
                database={"status": "ok"},
                capture=capture,
                credentials=credential_state,
                sources=sources,
                targets_truncated=len(ordered) > MAX_TARGETS or observed_truncated,
                known_gaps=[_public(g) for g in gaps[:MAX_GAPS]],
                known_gaps_truncated=len(gaps) > MAX_GAPS,
                known_gaps_window_days=7,
                known_gaps_continue=(
                    "GET /admin/alert-history/events?kind=history_gap (and kind=history_conflict)"
                ),
                retention="no automatic deletion",
            )
        )
    )


# -- events -------------------------------------------------------------------

_EVENT_FILTERS = (
    "source",
    "source_namespace",
    "deployment_id",
    "target_environment",
    "kind",
    "alert_type",
    "model_id",
    "endpoint_id",
    "channel",
    "outcome",
    "origin",
)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return dict(row)


@router.get("/events")
async def history_events(
    identity: ReaderIdentity = Depends(require_history_reader),
    since: dt.datetime | None = Query(None),
    until: dt.datetime | None = Query(None),
    source: str | None = Query(None),
    source_namespace: str | None = Query(None),
    deployment_id: str | None = Query(None),
    target_environment: str | None = Query(None),
    kind: str | None = Query(None),
    alert_type: str | None = Query(None),
    model_id: str | None = Query(None),
    endpoint_id: str | None = Query(None),
    channel: str | None = Query(None),
    outcome: str | None = Query(None),
    origin: str | None = Query(None),
    limit: int = Query(store.DEFAULT_PAGE, ge=1, le=store.MAX_PAGE),
    cursor: str | None = Query(None, max_length=512),
) -> JSONResponse:
    """Events that occurred in a window of at most 7 days, by recording timestamp.

    Paged by ``(recorded_at, record_id)``: the insert-time database clock,
    not commit order. A row committed after the cursor passed its timestamp is
    missed by the remaining pages; rescan the window and deduplicate by
    ``record_id`` to pick it up.
    """
    now = dt.datetime.now(dt.UTC)
    filters = {
        "source": source,
        "source_namespace": source_namespace,
        "deployment_id": deployment_id,
        "target_environment": target_environment,
        "kind": kind,
        "alert_type": alert_type,
        "model_id": model_id,
        "endpoint_id": endpoint_id,
        "channel": channel,
        "outcome": outcome,
        "origin": origin,
    }
    for value in filters.values():
        if value is not None and not _FILTER.fullmatch(value):
            return _error(422, "invalid_request", "A filter value is not acceptable.")
    # The query is what the caller asked for; a defaulted window is resolved
    # once, on the first page, and then travels inside the cursor.
    fingerprint = _fingerprint(identity, since=since, until=until, **filters)
    after = None
    carried = None
    if cursor:
        try:
            after, carried = store.decode_cursor(cursor, fingerprint)
        except ValueError:
            return _error(422, "invalid_request", "The cursor does not belong to this query.")
    window = _carried_window(carried, since, until) if carried else _window(since, until, now)
    if isinstance(window, JSONResponse):
        return window
    start, end = window
    runtime = _runtime_or_error()
    if isinstance(runtime, JSONResponse):
        return runtime
    if runtime.read_slots.locked():
        return _error(429, "too_many_requests", "Too many concurrent history reads.")
    try:
        async with runtime.read_slots, runtime.acquire() as conn:
            rows = await store.list_events(
                conn,
                scope=identity.scope,
                since=start,
                until=end,
                filters=filters,
                after=after,
                limit=limit,
            )
            page, next_cursor = _page(rows, limit, "recorded_at", fingerprint, (start, end))
            pairs = {(r["source"], r["source_namespace"]) for r in page}
            if source and source_namespace:
                pairs.add((source, source_namespace))
            coverage = await _coverage_for(conn, identity, pairs)
            gaps = await store.known_gaps(conn, scope=identity.scope, since=start, until=end)
    except Exception as exc:
        return _unavailable(exc)
    return JSONResponse(
        _json_ready(
            _envelope(
                now,
                window={"since": start, "until": end, "filtered_on": "occurred_at"},
                order="recorded_at, record_id",
                pagination=PAGINATION_NOTE,
                events=[_public(r) for r in page],
                next_cursor=next_cursor,
                truncated=next_cursor is not None,
                coverage=coverage,
                known_gaps=[_public(g) for g in gaps],
            )
        )
    )


async def _timeline(
    identity: ReaderIdentity,
    match: dict[str, Any],
    limit: int,
    cursor: str | None,
    **extra: Any,
) -> JSONResponse:
    now = dt.datetime.now(dt.UTC)
    fingerprint = _fingerprint(identity, **match)
    after = None
    if cursor:
        try:
            after, _ = store.decode_cursor(cursor, fingerprint)
        except ValueError:
            return _error(422, "invalid_request", "The cursor does not belong to this query.")
    runtime = _runtime_or_error()
    if isinstance(runtime, JSONResponse):
        return runtime
    if runtime.read_slots.locked():
        return _error(429, "too_many_requests", "Too many concurrent history reads.")
    try:
        async with runtime.read_slots, runtime.acquire() as conn:
            rows = await store.timeline(
                conn, scope=identity.scope, match=match, after=after, limit=limit
            )
            page, next_cursor = _page(rows, limit, "recorded_at", fingerprint)
            coverage = await _coverage_for(
                conn, identity, {(r["source"], r["source_namespace"]) for r in page}
            )
            # Over the whole timeline in scope, not this page: an end record
            # read on an earlier page is still an end record.
            summary = await store.timeline_summary(conn, scope=identity.scope, match=match)
    except Exception as exc:
        return _unavailable(exc)
    if not page and after is None:
        return _error(404, "not_found", "No recorded history matches (in your scope).")
    return JSONResponse(
        _json_ready(
            _envelope(
                now,
                order="recorded_at, record_id",
                pagination=PAGINATION_NOTE,
                display_order="occurred_at, producer_seq",
                events=[_public(r) for r in page],
                next_cursor=next_cursor,
                truncated=next_cursor is not None,
                coverage=coverage,
                # A missing end record means no end was recorded (yet: a late
                # arrival may still add one), not that the condition is still
                # on, and never that it recovered.
                # From the exact whole-timeline aggregate, never from the
                # (possibly truncated) per-generation list below.
                end_recorded=summary["end_recorded"],
                summary={
                    "scope": "whole timeline, committed rows in your scope",
                    "records": summary["records"],
                    "trigger_recorded": summary["trigger_recorded"],
                    "end_recorded": summary["end_recorded"],
                    "generation_count": summary["generation_count"],
                    "generations": summary["generations"],
                    "generations_truncated": summary["generations_truncated"],
                },
                **extra,
            )
        )
    )


@router.get("/episodes/{episode_id}")
async def history_episode(
    episode_id: str,
    identity: ReaderIdentity = Depends(require_history_reader),
    limit: int = Query(store.MAX_PAGE, ge=1, le=store.MAX_PAGE),
    cursor: str | None = Query(None, max_length=512),
) -> JSONResponse:
    """One backend episode's recorded timeline (one process's observation)."""
    if not _FILTER.fullmatch(episode_id):
        return _error(422, "invalid_request", "Not an episode id.")
    return await _timeline(
        identity,
        {"episode_id": episode_id, "source": "backend"},
        limit,
        cursor,
        episode_id=episode_id,
        observation_scope="process",
    )


@router.get("/source-incidents/{source}/{incident_id}")
async def history_source_incident(
    source: str,
    incident_id: str,
    identity: ReaderIdentity = Depends(require_history_reader),
    source_namespace: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(store.MAX_PAGE, ge=1, le=store.MAX_PAGE),
    cursor: str | None = Query(None, max_length=512),
) -> JSONResponse:
    """A source incident's history across generations, within one namespace."""
    for value in (source, incident_id, source_namespace):
        if not _FILTER.fullmatch(value):
            return _error(422, "invalid_request", "Not an incident reference.")
    return await _timeline(
        identity,
        {
            "source": source,
            "source_namespace": source_namespace,
            "source_incident_id": incident_id,
        },
        limit,
        cursor,
        source_incident_id=incident_id,
    )


@router.get("/request-evidence/{request_id}")
async def history_request_evidence(
    request_id: str,
    identity: ReaderIdentity = Depends(require_history_reader),
) -> JSONResponse:
    """Limited metadata of a request that recorded history names, owner-checked.

    Only a request id that a backend history row in the caller's scope
    references can be looked up; anything else is ``404`` whether or not the
    request exists, so this is not a way to browse ``api_logs``.
    """
    if not _REQUEST_ID.fullmatch(request_id):
        return _error(422, "invalid_request", "Not a request id.")
    now = dt.datetime.now(dt.UTC)
    runtime = _runtime_or_error()
    if isinstance(runtime, JSONResponse):
        return runtime
    if not runtime.fence_secret:
        return _error(
            503,
            "history_unavailable",
            "Request evidence needs the erasure fence; it is not set up.",
        )
    if runtime.read_slots.locked():
        return _error(429, "too_many_requests", "Too many concurrent history reads.")
    try:
        async with runtime.read_slots, runtime.acquire() as conn:
            refs = await store.events_referencing_request(
                conn, scope=identity.scope, request_id=request_id
            )
            if not refs:
                return _error(404, "not_referenced", "No history in your scope references it.")
            owners = {owner for ref in refs for owner in ref["owner_digests"]}
            state, evidence = await store.request_evidence(
                conn, request_id=request_id, owner_digests=owners, fence_secret=runtime.fence_secret
            )
    except Exception as exc:
        return _unavailable(exc)
    return JSONResponse(
        _json_ready(
            _envelope(
                now,
                request_id=request_id,
                available=state == "available",
                unavailable_reason=None if state == "available" else state,
                evidence=evidence,
                referenced_by=[{k: v for k, v in r.items() if k != "owner_digests"} for r in refs],
                fields="timestamp, model_id, endpoint_id, status_code, latency_ms, error_class",
            )
        )
    )
