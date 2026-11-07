"""``POST /internal/alert-history/events``: history from sources other than the backend.

The answer is ``200`` only after the transaction holding the batch committed.
Nothing is queued in memory here: a request that cannot be committed now is
answered with a retryable error, and the source resends the same events with
the same ids. Contract: ``docs/developer/alert-history.md``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from serving.observability.alert_history.records import MAX_RECORD_BYTES, utc_iso
from serving.observability.alert_history.redaction import sanitize_facts
from serving.observability.alert_history.runtime import HistoryUnavailable, get_runtime
from serving.observability.alert_history.wire import (
    MAX_BODY_BYTES,
    MAX_FUTURE,
    MAX_PAST,
    IngestRequest,
    to_record,
)
from serving.servers.routers.alert_history_auth import WriterCredential, require_history_writer
from serving.storage import alert_history_store as store
from serving.utils.logging import get_logger

router = APIRouter(prefix="/internal/alert-history", tags=["Internal"])
logger = get_logger(__name__)

#: Whole-request budget for the commit; past it the source retries.
COMMIT_TIMEOUT_SEC = 10.0
#: Budget for receiving the body, separate from the commit's.
BODY_READ_TIMEOUT_SEC = 10.0
#: Deepest JSON nesting the contract can produce is 4; anything far beyond is
#: refused before the parser recurses into it.
MAX_JSON_DEPTH = 16


def _too_deep(body: bytes) -> bool:
    """Whether brackets nest deeper than :data:`MAX_JSON_DEPTH` (strings skipped)."""
    depth = 0
    in_string = False
    escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return False


def _error(status_code: int, error_type: str, message: str, **extra: Any) -> JSONResponse:
    headers = {}
    if status_code in (429, 503):
        headers["Retry-After"] = "5"
    return JSONResponse(
        {"error": {"type": error_type, "message": message, **extra}},
        status_code=status_code,
        headers=headers,
    )


async def _bounded_body(request: Request) -> bytes | None:
    """The raw body, or ``None`` once it exceeds the limit (never buffered past it)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _details(exc: ValidationError) -> list[dict[str, Any]]:
    """Field paths and codes only: the submitted values are never echoed."""
    out = []
    for item in exc.errors()[:50]:
        loc = list(item.get("loc", ()))
        index = loc[1] if len(loc) > 1 and loc[0] == "events" and isinstance(loc[1], int) else None
        out.append(
            {
                "index": index,
                "field": ".".join(str(part) for part in loc),
                "code": str(item.get("type", "invalid")),
            }
        )
    return out


@router.post("/events")
async def ingest_events(
    request: Request,
    credential: WriterCredential = Depends(require_history_writer),
) -> JSONResponse:
    """Commit a batch of history events (and an optional source heartbeat)."""
    content_type = request.headers.get("content-type", "")
    if content_type.split(";")[0].strip().lower() != "application/json":
        return _error(415, "unsupported_media_type", "Send application/json.")
    # The body is read before any commit slot is taken, under its own budget,
    # so a slow sender cannot hold a database connection or stall the others.
    try:
        body = await asyncio.wait_for(_bounded_body(request), timeout=BODY_READ_TIMEOUT_SEC)
    except TimeoutError:
        return _error(408, "request_timeout", "The body was not received in time.")
    if body is None:
        return _error(413, "payload_too_large", f"Bodies are limited to {MAX_BODY_BYTES} bytes.")
    if _too_deep(body):
        return _error(422, "invalid_request", "The body is nested too deeply.", details=[])
    try:
        parsed = json.loads(body, parse_constant=_refuse_constant)
        payload = IngestRequest.model_validate(parsed)
    except ValidationError as exc:
        return _error(
            422,
            "invalid_request",
            "The request does not match the contract.",
            details=_details(exc),
        )
    except (ValueError, UnicodeDecodeError, RecursionError, TypeError):
        # Never logged with the input: a body is whatever the sender chose.
        return _error(422, "invalid_request", "The body is not valid JSON.", details=[])

    allowed = set(credential.target_environments)
    environments = {event.target_environment for event in payload.events}
    if payload.source_status is not None:
        environments.add(payload.source_status.target_environment)
    if not environments <= allowed:
        return _error(
            403, "scope_denied", "This credential may not report about that target environment."
        )

    now = dt.datetime.now(dt.UTC)
    problems: list[dict[str, Any]] = []
    status_in = payload.source_status
    if status_in is not None:
        for name in ("observed_at", "started_at", "pending_reported_at", "stopped_at"):
            value = getattr(status_in, name)
            if value is not None and not (now - MAX_PAST <= value <= now + MAX_FUTURE):
                problems.append(
                    {"index": None, "field": f"source_status.{name}", "code": "out_of_range"}
                )
    records = []
    for index, event in enumerate(payload.events):
        if not (now - MAX_PAST <= event.occurred_at <= now + MAX_FUTURE):
            problems.append({"index": index, "field": "occurred_at", "code": "out_of_range"})
            continue
        record = to_record(
            event,
            source=credential.source,
            source_namespace=credential.source_namespace,
            deployment_id=credential.deployment_id,
            credential_id=credential.id,
        )
        if record.size > MAX_RECORD_BYTES:
            problems.append({"index": index, "field": "", "code": "event_too_large"})
            continue
        records.append(record)
    ids = [record.origin_record_id for record in records]
    if len(set(ids)) != len(ids):
        problems.append({"index": None, "field": "origin_record_id", "code": "duplicate_in_batch"})
    if problems:
        return _error(
            422, "invalid_request", "Some events are not acceptable.", details=problems[:50]
        )

    snapshot = None
    if payload.source_status is not None:
        snapshot = _snapshot(payload.source_status, credential)

    runtime = get_runtime()
    if runtime is None:
        return _error(
            404, "alert_history_not_configured", "This deployment does not accept alert history."
        )
    if runtime.ingest_slots.locked():
        return _error(429, "too_many_requests", "Too many concurrent history submissions.")
    async with runtime.ingest_slots:
        try:
            results, recorded_at = await asyncio.wait_for(
                _commit(runtime, records, snapshot), timeout=COMMIT_TIMEOUT_SEC
            )
        except store.InvalidRecords as exc:
            return _error(
                422,
                "invalid_request",
                "The database refused some events; nothing was stored.",
                details=[{"index": i, "field": "", "code": "unstorable"} for i in exc.indexes[:50]],
            )
        except (HistoryUnavailable, TimeoutError, OSError, store.RetryableWriteError):
            return _error(
                503, "history_unavailable", "History could not be committed; retry the same body."
            )
        except Exception:
            logger.warning("alert history ingest failed", exc_info=True)
            return _error(
                503, "history_unavailable", "History could not be committed; retry the same body."
            )
    return JSONResponse(
        {
            "committed": True,
            "recorded_at": utc_iso(recorded_at),
            "source": credential.source,
            "source_namespace": credential.source_namespace,
            "results": [
                {
                    "index": index,
                    "origin_record_id": result.origin_record_id,
                    "status": result.status,
                    "record_id": result.record_id,
                }
                for index, result in enumerate(results)
            ],
            "source_status": "recorded" if snapshot is not None else "absent",
        },
        status_code=status.HTTP_200_OK,
    )


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _snapshot(source_status: Any, credential: WriterCredential) -> dict[str, Any]:
    # Source metadata is free text like any other: same redaction as facts.
    rules, _ = sanitize_facts(source_status.rules_snapshot)
    return {
        "source": credential.source,
        "source_namespace": credential.source_namespace,
        "source_instance_id": source_status.source_instance_id,
        "deployment_id": credential.deployment_id,
        "target_environment": source_status.target_environment,
        "release_id": source_status.release_id,
        "started_at": source_status.started_at,
        "observed_at": source_status.observed_at,
        "sync_watermark": source_status.sync_watermark,
        "pending_count": source_status.pending_count,
        "pending_reported_at": source_status.pending_reported_at,
        "dropped_count": source_status.dropped_count,
        "last_error_code": source_status.last_error_code,
        "last_error_at": source_status.observed_at if source_status.last_error_code else None,
        "rules_snapshot": rules if source_status.rules_snapshot is not None else None,
        "rules_snapshot_hash": source_status.rules_snapshot_hash,
        # A final snapshot: the invocation finished cleanly. Only this commit
        # confirms it; a later report never clears it (stop fields coalesce).
        "stopped_at": source_status.stopped_at,
        "stop_reason": source_status.stop_reason,
        "lost_at_stop": source_status.lost_at_stop,
        "credential_id": credential.id,
    }


async def _commit(
    runtime: Any, records: list[Any], snapshot: dict[str, Any] | None
) -> tuple[list[store.ItemResult], dt.datetime]:
    async with runtime.acquire() as conn, conn.transaction():
        await conn.execute(f"SET LOCAL statement_timeout = '{store.WRITE_STATEMENT_TIMEOUT}'")
        results = await store.write_records(conn, records, all_or_nothing=True)
        if snapshot is not None:
            await store.upsert_source(conn, snapshot, wrote_events=bool(records))
        recorded_at = await conn.fetchval("SELECT clock_timestamp()")
    # Reaching here means COMMIT returned: only now is anything "saved".
    return results, recorded_at
