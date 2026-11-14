"""Admin export endpoints — JSONL streaming of request logs."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from serving.servers.auth import log_admin_action
from serving.servers.deps import get_admin_audit_actor, get_db_logger
from serving.servers.routers.admin._common import (
    _escape_ilike_substring_term,
    resolve_request_outcome_filter,
    resolve_request_source_filter,
)
from serving.servers.streaming_state import STREAMING_RESPONSE_SCOPE_STATE_KEY
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator, Mapping

    from serving.audit import AuditActor

logger = get_logger(__name__)

router = APIRouter(prefix="/admin")

#: Characters of JSONL gathered before they are sent, and the event loop is
#: given back. A page of 500 rows with long prompts is 35-126 MB of JSON; built
#: as one string it held the loop for 114-375 ms.
_CHUNK_CHARS = 1024 * 1024

#: Characters of one long text field (a prompt, a response) escaped in one go,
#: so a single large row is serialized a slice at a time.
_FIELD_SLICE_CHARS = 256 * 1024

#: The text fields of a row with content, as the export writes them.
_TEXT_FIELDS = ("prompt", "response")
#: Its JSONB fields, written decoded.
_JSONB_FIELDS = ("tools", "metadata", "request_payload")


def _decode_jsonish(value: Any) -> Any:
    """Return JSONB values as objects even when asyncpg gives us strings."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        logger.warning("admin export: malformed JSONB string, returning raw")
        return value


def _json_pieces(value: Any) -> Iterator[str]:
    """``json.dumps(value)`` in pieces: a long string is escaped one slice at a time.

    Escaping works character by character, and a Python string is sliced by
    code point, so the pieces join to exactly what one call would return.
    """
    if not isinstance(value, str) or len(value) <= _FIELD_SLICE_CHARS:
        yield json.dumps(value)
        return
    yield '"'
    for start in range(0, len(value), _FIELD_SLICE_CHARS):
        yield json.dumps(value[start : start + _FIELD_SLICE_CHARS])[1:-1]
    yield '"'


def _record_pieces(row: Mapping[str, Any], *, include_content: bool) -> Iterator[str]:
    """One JSONL line for *row*, in pieces that join to its record's ``json.dumps`` and a newline.

    The line is the record the export has always written, field for field and
    byte for byte; only the work of writing it is split, so that no step of it
    is bigger than one slice of a text field or one JSONB value.
    """
    record: dict[str, Any] = {
        "request_id": row["request_id"],
        "timestamp": row["timestamp"].isoformat(),
        "user_id": row["user_id"],
        "user_name": row["user_name"],
        "user_email": row["user_email"],
        "model_id": row["model_id"],
        "provider": row["provider"],
        "ttft_ms": row["ttft_ms"],
        "latency_ms": row["latency_ms"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "reasoning_tokens": row["reasoning_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "cache_write_tokens": row["cache_write_tokens"],
        "total_tokens": row["total_tokens"],
        "cost_usd": (str(row["cost_usd"]) if row["cost_usd"] is not None else None),
        "status_code": row["status_code"],
        "error": row["error"],
        # The batch whose worker ran it; None for a direct request.
        "batch_job_id": row.get("batch_job_id"),
    }
    if not include_content:
        yield json.dumps(record) + "\n"
        return
    # The content fields follow the others, as json.dumps writes a dict: each
    # after ", ", its key, then ": ".
    yield json.dumps(record)[:-1]
    for name in _TEXT_FIELDS:
        yield f', "{name}": '
        yield from _json_pieces(row[name])
    for name in _JSONB_FIELDS:
        yield f', "{name}": '
        yield json.dumps(_decode_jsonish(row[name]))
    yield "}\n"


@router.get("/export/requests")
async def admin_export_requests(
    request: Request,
    start_time: datetime,
    end_time: datetime | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    model_id: str | None = None,
    errors_only: bool = False,
    outcome: str | None = None,
    request_type: str | None = None,
    source: str | None = None,
    batch_id: str | None = None,
    include_content: bool = False,
    actor: AuditActor = Depends(get_admin_audit_actor),
    db_logger=Depends(get_db_logger),
) -> StreamingResponse:
    """Stream all request logs matching the given filters as JSONL.

    Query Parameters:
    - start_time: ISO8601 datetime, inclusive lower bound (required)
    - end_time: ISO8601 datetime, inclusive upper bound (defaults to now)
    - user_id: Filter by user ID, name, or email (substring match)
    - session_id: Filter to one session (exact match on ``api_logs.session_id``)
    - model_id: Filter by model ID (substring match)
    - errors_only: If true, only include requests with errors. Equivalent to
      ``outcome=errors``; kept for existing callers
    - outcome: Outcome class to export, as on ``/admin/recent-requests``
      (``"all"``, ``"errors"``, ``"errors_excluding_disconnects"`` or
      ``"client_disconnect"``). Wins over ``errors_only``
    - request_type: ``"embedding"`` to export only embedding requests,
      ``"chat"`` to exclude them; any other value (or omission) applies no filter
    - source: ``"interactive"``, ``"batch"`` or ``"all"``, as on
      ``/admin/recent-requests``; any other value is a 422
    - batch_id: Export one batch's requests (exact match)
    - include_content: If true, include prompt and response fields

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    if start_time.tzinfo is None:
        raise HTTPException(422, "start_time must be timezone-aware (e.g. 2024-01-01T00:00:00Z)")

    if end_time is None:
        end_time = datetime.now(UTC)
    elif end_time.tzinfo is None:
        raise HTTPException(422, "end_time must be timezone-aware (e.g. 2024-01-01T00:00:00Z)")

    where_clauses: list[str] = ["l.timestamp >= $1", "l.timestamp <= $2"]
    params: list[Any] = [start_time, end_time]

    # Filters mirror /admin/recent-requests so the JSONL export matches exactly
    # what the admin sees in the Requests tab (which reuses the same filter
    # values): a substring match across user id/name/email/login name, and a
    # substring match on model id. The SELECT below already joins users.
    if user_id:
        params.append(_escape_ilike_substring_term(user_id))
        idx = len(params)
        login_name = "u.login_name"
        where_clauses.append(
            f"(l.user_id ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR u.user_name ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR u.email ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR {login_name} ILIKE '%' || ${idx} || '%' ESCAPE '\\')"
        )

    if session_id:
        # Exact, and on the indexed column — same predicate as the list view, so
        # a session-scoped export is the rows the admin is looking at and not a
        # superset of them.
        params.append(session_id)
        where_clauses.append(f"l.session_id = ${len(params)}")

    if model_id:
        params.append(_escape_ilike_substring_term(model_id))
        where_clauses.append(f"l.model_id ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'")

    # Same outcome vocabulary as the list view, so exporting the rows an admin
    # has narrowed to "client disconnects" yields those rows and not the whole
    # error stream. Constant predicate — no bind param to renumber.
    outcome_sql = resolve_request_outcome_filter(outcome, errors_only=errors_only)
    if outcome_sql:
        where_clauses.append(outcome_sql)

    # Mirror the /admin/recent-requests request-type filter. Constant predicate
    # (no bind param), so it composes with the cursor/limit placeholders added
    # per batch below.
    if request_type == "embedding":
        where_clauses.append("(l.metadata->>'request_type') = 'embedding'")
    elif request_type == "chat":
        where_clauses.append("(l.metadata->>'request_type') IS DISTINCT FROM 'embedding'")

    # And its source filter: exporting from Recent Requests takes the requests
    # callers made, from the Batches tab the batch worker's. Constant predicate.
    source_sql = resolve_request_source_filter(source)
    if source_sql:
        where_clauses.append(source_sql)

    if batch_id:
        params.append(batch_id)
        where_clauses.append(f"l.batch_job_id = ${len(params)}")

    content_cols = (
        ", l.prompt, l.response, l.tools, l.metadata, l.request_payload" if include_content else ""
    )
    batch_size = 500

    start_str = start_time.strftime("%Y%m%d")
    end_str = end_time.strftime("%Y%m%d")
    filename = f"requests-{start_str}-{end_str}.jsonl"

    # Recorded when the export is requested, not when its stream ends: a
    # download the client drops is cancelled with its generator, and an audit
    # write there would be cancelled with it.
    await log_admin_action(
        db_logger,
        action="export_requests",
        actor=actor,
        details={
            "range": f"{start_str}-{end_str}",
            "include_content": include_content,
            "user_id": user_id,
            # Which conversation's requests were taken. Without it two
            # session-scoped exports with otherwise identical filters
            # are indistinguishable in the audit log, which exists to
            # record exactly what was accessed.
            "session_id": session_id,
            "model_id": model_id,
            "errors_only": errors_only,
            "outcome": outcome,
            "request_type": request_type,
            "source": source,
            "batch_id": batch_id,
        },
    )

    async def generate() -> AsyncGenerator[str, None]:
        # A connection is held for one page's query and released before the
        # page is sent: the client reads at its own pace, and a slow or stalled
        # download must not keep a pooled connection out of everyone else's
        # reach for as long as it takes. The keyset cursor carries the position
        # from one page to the next, so nothing depends on the connection.
        cursor_ts: datetime | None = None
        cursor_id: str | None = None
        while True:
            local_clauses = list(where_clauses)
            local_params = list(params)
            if cursor_ts is not None:
                cursor_ts_idx = len(local_params) + 1
                cursor_id_idx = len(local_params) + 2
                local_clauses.append(
                    f"(l.timestamp, l.request_id) < (${cursor_ts_idx}, ${cursor_id_idx})"
                )
                local_params.append(cursor_ts)
                local_params.append(cursor_id)
            limit_idx = len(local_params) + 1
            local_where = "WHERE " + " AND ".join(local_clauses)
            async with db_logger.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT
                        l.request_id, l.user_id, u.user_name, u.email AS user_email,
                        l.model_id, l.provider, l.timestamp,
                        l.status_code, l.latency_ms, l.ttft_ms,
                        l.prompt_tokens, l.completion_tokens, l.reasoning_tokens,
                        l.cache_read_tokens, l.cache_write_tokens, l.total_tokens,
                        l.cost_usd, l.error, l.batch_job_id{content_cols}
                    FROM api_logs l
                    LEFT JOIN users u ON u.id = l.user_id
                    {local_where}
                    ORDER BY l.timestamp DESC, l.request_id DESC
                    LIMIT ${limit_idx}
                    """,
                    *local_params,
                    batch_size,
                )
            if not rows:
                break
            # Sent about a MiB at a time, each chunk built from rows written a
            # piece at a time (_record_pieces), with the event loop given back
            # after each: however fast the client reads, and however long the
            # prompts, no step holds the loop for more than one chunk's work.
            pieces: list[str] = []
            gathered = 0
            for row in rows:
                for piece in _record_pieces(row, include_content=include_content):
                    pieces.append(piece)
                    gathered += len(piece)
                    if gathered >= _CHUNK_CHARS:
                        yield "".join(pieces)
                        pieces, gathered = [], 0
                        await asyncio.sleep(0)
            if pieces:
                yield "".join(pieces)
            if len(rows) < batch_size:
                break
            cursor_ts = rows[-1]["timestamp"]
            cursor_id = rows[-1]["request_id"]

    # Marked as the long-lived stream it is. ``TimeoutMiddleware`` ends a
    # response at ``REQUEST_TIMEOUT_SECONDS`` (120 s by default) even after its
    # status went out, and knows a stream only by this mark or by an SSE
    # content type; unmarked, an export that takes longer stops short behind a
    # ``200``. Marked, it gets the stream cap
    # (``STREAM_REQUEST_TIMEOUT_SECONDS``) instead, as a batch download does.
    request.scope.setdefault("state", {})[STREAMING_RESPONSE_SCOPE_STATE_KEY] = True
    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )
