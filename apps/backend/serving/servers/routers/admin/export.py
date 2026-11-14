"""Admin export endpoints — JSONL streaming of request logs.

``GET /admin/export/requests`` streams an export to a caller that authenticates
with an ``Authorization`` header: a script. The console cannot hand such a
response to the browser's own download without first holding all of it, so it
asks ``POST /admin/export/requests/link`` for a short-lived signed link and
lets the browser follow it to ``GET /admin/export/requests/download``, which
the browser saves to its downloads as it arrives.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    admit_admin_session,
    get_admin_audit_actor,
    get_admin_session_id,
    get_admin_user_id,
    get_db_logger,
    get_operational_store,
)
from serving.servers.routers.admin._common import (
    _escape_ilike_substring_term,
    resolve_request_outcome_filter,
    resolve_request_source_filter,
)
from serving.servers.streaming_state import STREAMING_RESPONSE_SCOPE_STATE_KEY
from serving.utils.jwt import get_jwt_secret
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

#: How long a download link lasts. It only has to reach the browser's request
#: for it, issued the moment the console receives it; a download that has
#: started runs on however long it takes. Short, because the link is a URL and
#: so lands in access logs.
LINK_TTL_SECONDS = 60
#: The ``aud`` of a download link, which no access token carries.
_LINK_AUDIENCE = "hybridinference/admin-export-download"
#: Domain separation: no other use of JWT_SECRET_KEY derives this key.
_LINK_KEY_LABEL = b"hybridinference/admin-export-link/v1"


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


@dataclass(frozen=True, slots=True)
class ExportFilters:
    """Which request logs one export takes.

    Built from the query of a direct export or a link request, and carried in
    a download link's claims, so the rows a link downloads are the ones its
    admin asked for when it was issued.
    """

    start_time: datetime
    end_time: datetime
    user_id: str | None = None
    session_id: str | None = None
    model_id: str | None = None
    errors_only: bool = False
    outcome: str | None = None
    request_type: str | None = None
    source: str | None = None
    batch_id: str | None = None
    include_content: bool = False

    def to_claims(self) -> dict[str, Any]:
        """The filters as JSON values, the times in ISO 8601."""
        claims = {name.name: getattr(self, name.name) for name in fields(self)}
        claims["start_time"] = self.start_time.isoformat()
        claims["end_time"] = self.end_time.isoformat()
        return claims

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> ExportFilters:
        """The filters :meth:`to_claims` wrote."""
        values = {name.name: claims.get(name.name) for name in fields(cls)}
        values["start_time"] = datetime.fromisoformat(claims["start_time"])
        values["end_time"] = datetime.fromisoformat(claims["end_time"])
        values["errors_only"] = bool(values["errors_only"])
        values["include_content"] = bool(values["include_content"])
        return cls(**values)


def export_filters(
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
) -> ExportFilters:
    """Read an export's filters from the query.

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
    """
    if start_time.tzinfo is None:
        raise HTTPException(422, "start_time must be timezone-aware (e.g. 2024-01-01T00:00:00Z)")
    if end_time is None:
        end_time = datetime.now(UTC)
    elif end_time.tzinfo is None:
        raise HTTPException(422, "end_time must be timezone-aware (e.g. 2024-01-01T00:00:00Z)")
    filters = ExportFilters(
        start_time=start_time,
        end_time=end_time,
        user_id=user_id,
        session_id=session_id,
        model_id=model_id,
        errors_only=errors_only,
        outcome=outcome,
        request_type=request_type,
        source=source,
        batch_id=batch_id,
        include_content=include_content,
    )
    # Refuse a filter it cannot apply now, before an export or a link starts.
    _where(filters)
    return filters


def _where(filters: ExportFilters) -> tuple[list[str], list[Any]]:
    """The WHERE clauses and bind parameters that select *filters*' rows.

    Raises:
        HTTPException: 422 for a ``source`` the list view does not know.
    """
    where_clauses: list[str] = ["l.timestamp >= $1", "l.timestamp <= $2"]
    params: list[Any] = [filters.start_time, filters.end_time]

    # Filters mirror /admin/recent-requests so the JSONL export matches exactly
    # what the admin sees in the Requests tab (which reuses the same filter
    # values): a substring match across user id/name/email/login name, and a
    # substring match on model id. The SELECT below already joins users.
    if filters.user_id:
        params.append(_escape_ilike_substring_term(filters.user_id))
        idx = len(params)
        login_name = "u.login_name"
        where_clauses.append(
            f"(l.user_id ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR u.user_name ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR u.email ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR {login_name} ILIKE '%' || ${idx} || '%' ESCAPE '\\')"
        )

    if filters.session_id:
        # Exact, and on the indexed column — same predicate as the list view, so
        # a session-scoped export is the rows the admin is looking at and not a
        # superset of them.
        params.append(filters.session_id)
        where_clauses.append(f"l.session_id = ${len(params)}")

    if filters.model_id:
        params.append(_escape_ilike_substring_term(filters.model_id))
        where_clauses.append(f"l.model_id ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'")

    # Same outcome vocabulary as the list view, so exporting the rows an admin
    # has narrowed to "client disconnects" yields those rows and not the whole
    # error stream. Constant predicate — no bind param to renumber.
    outcome_sql = resolve_request_outcome_filter(filters.outcome, errors_only=filters.errors_only)
    if outcome_sql:
        where_clauses.append(outcome_sql)

    # Mirror the /admin/recent-requests request-type filter. Constant predicate
    # (no bind param), so it composes with the cursor/limit placeholders added
    # per batch below.
    if filters.request_type == "embedding":
        where_clauses.append("(l.metadata->>'request_type') = 'embedding'")
    elif filters.request_type == "chat":
        where_clauses.append("(l.metadata->>'request_type') IS DISTINCT FROM 'embedding'")

    # And its source filter: exporting from Recent Requests takes the requests
    # callers made, from the Batches tab the batch worker's. Constant predicate.
    source_sql = resolve_request_source_filter(filters.source)
    if source_sql:
        where_clauses.append(source_sql)

    if filters.batch_id:
        params.append(filters.batch_id)
        where_clauses.append(f"l.batch_job_id = ${len(params)}")

    return where_clauses, params


async def _export_response(
    request: Request,
    filters: ExportFilters,
    *,
    actor: AuditActor,
    db_logger: Any,
    via: str,
) -> StreamingResponse:
    """Audit an export and stream its rows as JSONL.

    *via* says in the audit row how the export was asked for: ``"direct"`` for
    ``GET /admin/export/requests``, ``"download_link"`` for a link the console
    had issued.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    where_clauses, params = _where(filters)
    include_content = filters.include_content
    content_cols = (
        ", l.prompt, l.response, l.tools, l.metadata, l.request_payload" if include_content else ""
    )
    batch_size = 500

    start_str = filters.start_time.strftime("%Y%m%d")
    end_str = filters.end_time.strftime("%Y%m%d")
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
            "user_id": filters.user_id,
            # Which conversation's requests were taken. Without it two
            # session-scoped exports with otherwise identical filters
            # are indistinguishable in the audit log, which exists to
            # record exactly what was accessed.
            "session_id": filters.session_id,
            "model_id": filters.model_id,
            "errors_only": filters.errors_only,
            "outcome": filters.outcome,
            "request_type": filters.request_type,
            "source": filters.source,
            "batch_id": filters.batch_id,
            "via": via,
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
            # Request logs with their prompts: kept by no cache on the way.
            "Cache-Control": "no-store",
        },
    )


@router.get("/export/requests")
async def admin_export_requests(
    request: Request,
    filters: ExportFilters = Depends(export_filters),
    actor: AuditActor = Depends(get_admin_audit_actor),
    db_logger=Depends(get_db_logger),
) -> StreamingResponse:
    """Stream all request logs matching the given filters as JSONL.

    Takes the query parameters :func:`export_filters` reads. For scripts: the
    console downloads through :func:`admin_export_request_link` instead, so the
    browser saves the file as it arrives.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    return await _export_response(request, filters, actor=actor, db_logger=db_logger, via="direct")


@router.post("/export/requests/link")
async def admin_export_request_link(
    request: Request,
    filters: ExportFilters = Depends(export_filters),
    _actor: AuditActor = Depends(get_admin_audit_actor),
    db_logger=Depends(get_db_logger),
) -> dict[str, Any]:
    """Issue a short-lived link that downloads this export.

    The browser downloads a file itself only from a plain URL, which it
    requests without the console's ``Authorization`` header. The link stands
    in for that header: it is signed, names this admin's account and session
    and these filters, and lasts :data:`LINK_TTL_SECONDS` -- long enough to
    reach the browser's request for it. A download that has started runs on
    regardless. Following it is audited as the export itself.

    Takes the query parameters :func:`export_filters` reads.

    Requires: an admin signed in with a session. ``ADMIN_TOKEN`` has no
    session to bind a link to; a script downloads
    ``GET /admin/export/requests`` directly.

    Returns:
        ``url``, the link as a path on the gateway, and ``expires_in``, its
        lifetime in seconds.
    """
    user_id = get_admin_user_id(request)
    session_id = get_admin_session_id(request)
    if not user_id or not session_id:
        raise HTTPException(
            403,
            "A download link is issued to a signed-in admin account. "
            "With ADMIN_TOKEN, request GET /admin/export/requests directly.",
        )
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")
    try:
        token = mint_export_link(user_id=user_id, session_id=session_id, filters=filters)
    except ValueError as exc:
        raise HTTPException(
            503, "Download links are unavailable: JWT_SECRET_KEY is not set."
        ) from exc
    return {
        "url": f"{router.prefix}/export/requests/download?token={token}",
        "expires_in": LINK_TTL_SECONDS,
    }


@router.get("/export/requests/download")
async def admin_export_request_download(
    request: Request,
    token: str,
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> StreamingResponse:
    """Download the export a link from :func:`admin_export_request_link` names.

    Carries no ``Authorization`` header: the browser follows the link itself.
    The link's signature and lifetime are checked, then its account and
    session exactly as on any admin request (:func:`admit_admin_session`), so
    an admin who signed out or lost the role in the meantime is refused.
    """
    try:
        claims = read_export_link(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            401, "This download link has expired. Start the export again from the console."
        ) from exc
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(401, "This download link is not valid.") from exc
    await admit_admin_session(request, op_store, {"sub": claims["sub"], "sid": claims["sid"]})
    filters = ExportFilters.from_claims(claims["f"])
    return await _export_response(
        request,
        filters,
        actor=get_admin_audit_actor(request),
        db_logger=db_logger,
        via="download_link",
    )


def _link_key() -> bytes:
    """The key that signs download links, derived from ``JWT_SECRET_KEY``.

    Its own key, so that no access token is a link and no link an access
    token, although both are signed JWTs from one secret.

    Raises:
        ValueError: When ``JWT_SECRET_KEY`` is not set.
    """
    return hmac.new(get_jwt_secret().encode(), _LINK_KEY_LABEL, hashlib.sha256).digest()


def mint_export_link(*, user_id: str, session_id: str, filters: ExportFilters) -> str:
    """Sign a download link token for *filters*, issued to one admin session."""
    now = datetime.now(UTC)
    claims = {
        "aud": _LINK_AUDIENCE,
        "sub": user_id,
        "sid": session_id,
        "iat": now,
        "exp": now + timedelta(seconds=LINK_TTL_SECONDS),
        "f": filters.to_claims(),
    }
    return jwt.encode(claims, _link_key(), algorithm="HS256")


def read_export_link(token: str) -> dict[str, Any]:
    """Verify a download link token and return its claims.

    Raises:
        jwt.ExpiredSignatureError: The link has expired.
        jwt.InvalidTokenError: It is not a link this gateway signed.
        ValueError: ``JWT_SECRET_KEY`` is not set.
    """
    claims = jwt.decode(
        token,
        _link_key(),
        algorithms=["HS256"],
        audience=_LINK_AUDIENCE,
        options={"require": ["exp", "sub", "sid", "f"]},
    )
    if not isinstance(claims.get("f"), dict):
        raise jwt.InvalidTokenError("link carries no filters")
    return claims
