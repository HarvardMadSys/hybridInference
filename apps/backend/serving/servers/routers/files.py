"""Files for OpenAI file batches (``/v1/files``, ``purpose=batch`` only).

The minimum the OpenAI Batch flow needs: upload a JSONL input (``POST``), list,
retrieve, download (``GET /v1/files/{id}/content``) and delete. Output and
error files of sealed batches (``purpose=batch_output``) appear here too; they
are rendered from the batch's items on download (``serving.batch.delivery``)
and match the size and digest their manifest recorded when the batch was sealed.

An upload streams into the database in chunks while its size and SHA-256 are
computed (``serving.batch.multipart``); it is usable only once its last byte
is stored and the upload committed. Uploads count against the owner's
storage cap (``BATCH_FILES_MAX_TOTAL_MB_PER_USER``) and upload rate
(``BATCH_FILE_UPLOADS_PER_HOUR``), and need the role ``BATCH_MIN_ROLE``;
reading and deleting do not, and are not held to
the daily cost quota. Deleting a file hides it at once; an input still being
validated keeps its bytes until validation ends, and a deleted output file is
never re-created.

An administrator may read and delete any owner's file by id. Each such access
to another owner's file is audited against that owner, and an upload's content
-- the owner's prompts -- is readable only where administrators may read
prompts (``db_store_full_content``), as on the admin console.

This is not a general Files API: a ``batch`` file cannot be referenced from a
chat, Messages or Responses request.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from serving.audit import AuditActor, AuditTarget
from serving.batch.admission import declared_length_exceeds
from serving.batch.config import batch_config, may_use_batch
from serving.batch.delivery import DownloadInterrupted, stream_lines, stream_upload
from serving.batch.multipart import MultipartError, boundary_of, limited, read_multipart
from serving.servers.auth import (
    log_admin_action,
    reads_every_owner,
    verify_batch_management_identity,
)
from serving.servers.deps import (
    get_batch_file_store,
    get_batch_store,
    get_log_store,
    get_operational_store,
)
from serving.servers.streaming_state import STREAMING_RESPONSE_SCOPE_STATE_KEY
from serving.storage.batch_file_store import (
    CHUNK_BYTES,
    BatchFileOwnerUnavailable,
    BatchFileQuotaExceeded,
)
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)

_UNAVAILABLE = "Files are not available on this gateway"
_MULTIPART_OVERHEAD = 64 * 1024


def _require_surface(
    store: Any = Depends(get_batch_store), file_store: Any = Depends(get_batch_file_store)
) -> None:
    if store is None or file_store is None or not batch_config().enabled:
        raise HTTPException(404, _UNAVAILABLE)


def _reject_on_behalf_of(request: Request) -> None:
    """Refuse ``X-On-Behalf-Of`` (the identity dependency does too; this holds without it).

    Work stored here runs later under the key that created it, re-checked
    before each request. A service key acting for an end user would store work
    owned by one identity and authorised by another.
    """
    if request.headers.get("x-on-behalf-of"):
        raise HTTPException(
            400,
            "X-On-Behalf-Of is not supported on /v1/files: "
            "a batch belongs to the key that creates it",
        )


router = APIRouter(dependencies=[Depends(_require_surface), Depends(_reject_on_behalf_of)])


def _owner(user_ctx: dict[str, Any]) -> str:
    return user_ctx.get("user_id") or "anonymous"


def _lookup_scope(user_ctx: dict[str, Any]) -> str | None:
    """Return the owner a lookup is restricted to: the caller, or none for an admin.

    An administrator who authenticated as one, that is: the anonymous caller of
    an auth-disabled gateway holds the admin role too (:func:`reads_every_owner`).
    """
    return None if reads_every_owner(user_ctx) else _owner(user_ctx)


def require_prompts_readable(log_store: Any) -> None:
    """Refuse an administrator stored prompts unless the request log keeps prompts.

    ``db_store_full_content`` already decides whether administrators can read
    request content: where a deployment keeps it off, the requests a batch
    stores stay unreadable to them too, although the batch needs them to run.

    Raises:
        HTTPException: 403 when it is off.
    """
    if not getattr(log_store, "store_full_prompts", False):
        raise HTTPException(
            status_code=403,
            detail=(
                "This gateway does not keep prompts for administrators to read "
                "(db_store_full_content is off)."
            ),
        )


async def audit_other_owner(
    request: Request,
    op_store: Any,
    user_ctx: dict[str, Any],
    owner: str,
    *,
    action: str,
    details: dict[str, Any],
    success: bool = True,
) -> None:
    """Audit an administrator's access to another owner's batch or file, against that owner.

    Only the administrator scope (:func:`_lookup_scope`) reaches another
    owner's work, so a caller's own batches and files are never audited. A
    read is audited before anything is returned: one whose row cannot be
    written is not answered. A change is audited before it is made
    (:func:`audited_change`). The key it came through is in the details.

    Raises:
        HTTPException: 500 without an operational store, which writes the
            row. ``log_admin_action`` would skip it in silence, and the access
            would go unaudited. (An administrator authenticates through that
            store, so no deployment reaches this today; the guarantee does
            not rest on that.)
    """
    caller = _owner(user_ctx)
    if owner == caller:
        return
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    await log_admin_action(
        op_store,
        action=action,
        actor=AuditActor.admin(caller, get_client_ip(request)),
        target=AuditTarget.user(owner),
        details={**details, "api_key_id": user_ctx.get("api_key_id")},
        success=success,
    )


@asynccontextmanager
async def audited_change(
    request: Request,
    op_store: Any,
    user_ctx: dict[str, Any],
    owner: str,
    *,
    action: str,
    details: dict[str, Any],
) -> AsyncIterator[None]:
    """Audit an administrator's change to another owner's batch or file before it is made.

    A cancel or a delete cannot be taken back, so its row is written first,
    as a read's is: one whose row cannot be written is not made (500), and
    no change to another owner's work goes unrecorded. Written afterwards, a
    failed write would leave the change made, the administrator told it
    failed, and nothing on record. A change the block then refuses -- an
    ``HTTPException``, such as the 409 of a batch still running or the 404 of
    one deleted meanwhile -- adds a second row, ``success`` false with the
    status code, so the log does not claim a change that was not made. A
    caller's own work is neither audited nor held up.
    """
    await audit_other_owner(request, op_store, user_ctx, owner, action=action, details=details)
    try:
        yield
    except HTTPException as exc:
        try:
            await audit_other_owner(
                request,
                op_store,
                user_ctx,
                owner,
                action=action,
                details={**details, "error": f"not made (HTTP {exc.status_code})"},
                success=False,
            )
        except Exception:
            # The refusal still stands; the first row records the attempt.
            logger.exception(
                "batch_admin_refusal_audit_failed",
                extra={"event": "batch_admin_refusal_audit_failed", "action": action},
            )
        raise


def _epoch(value: Any) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None


def file_object(row: dict[str, Any]) -> dict[str, Any]:
    """The OpenAI ``file`` object of a stored file."""
    return {
        "id": row["id"],
        "object": "file",
        "bytes": int(row.get("bytes") or 0),
        "created_at": _epoch(row.get("created_at")) or 0,
        "expires_at": _epoch(row.get("expires_at")),
        "filename": row.get("filename"),
        "purpose": row.get("purpose"),
        "status": "processed",
        "status_details": None,
    }


def safe_filename(name: str) -> str:
    """A stored filename: printable, no path or quoting characters, at most 255 characters."""
    cleaned = "".join(
        char if char.isprintable() and char not in '"\\/' else "_" for char in name
    ).strip()
    return cleaned[:255] or "upload.jsonl"


def content_disposition(name: str) -> str:
    """An attachment header for *name*: an ASCII fallback plus the RFC 5987 UTF-8 form."""
    clean = safe_filename(name)
    ascii_name = clean.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(clean, safe='')}"


def jsonl_download(
    request: Request, body: AsyncIterator[bytes], *, headers: dict[str, str]
) -> StreamingResponse:
    """A JSONL download, marked as the long-lived stream it is.

    ``TimeoutMiddleware`` ends a response at ``REQUEST_TIMEOUT_SECONDS``
    (120 s by default) even after its status went out, so a large file would
    stop short behind a ``200``. Marked as streaming, a download gets the
    stream cap (``STREAM_REQUEST_TIMEOUT_SECONDS``) instead, as SSE does.
    """
    request.scope.setdefault("state", {})[STREAMING_RESPONSE_SCOPE_STATE_KEY] = True
    return StreamingResponse(body, media_type="application/jsonl", headers=headers)


def _not_found(file_id: str) -> HTTPException:
    return HTTPException(404, f"File '{file_id}' not found")


class _Upload:
    """Writes the file part of an upload to the store, a chunk at a time."""

    def __init__(self, file_store: Any, *, file_id: str, owner: str, limit: int, cap: int) -> None:
        self._store = file_store
        self._file_id = file_id
        self._owner = owner
        self._limit = limit
        self._cap = cap
        self._pending = bytearray()
        self._seq = 0
        self.size = 0
        self.lines = 0
        self.ends_with_newline = True
        self.digest = hashlib.sha256()
        self.seen = False

    async def start(self, name: str, filename: str) -> None:
        if name != "file" or self.seen:
            raise MultipartError("send exactly one file part, named 'file'")
        self.seen = True
        self.filename = safe_filename(filename or "upload.jsonl")

    async def data(self, data: bytes) -> None:
        self.size += len(data)
        if self.size > self._limit:
            raise HTTPException(
                413, f"The file exceeds the batch size limit of {self._limit // (1024 * 1024)} MiB"
            )
        self.digest.update(data)
        self.lines += data.count(b"\n")
        self.ends_with_newline = data.endswith(b"\n")
        self._pending += data
        while len(self._pending) >= CHUNK_BYTES:
            await self._write(bytes(self._pending[:CHUNK_BYTES]))
            del self._pending[:CHUNK_BYTES]

    async def end(self) -> None:
        if self._pending:
            await self._write(bytes(self._pending))
            self._pending.clear()

    async def _write(self, chunk: bytes) -> None:
        await self._store.append_chunk(
            file_id=self._file_id,
            user_id=self._owner,
            seq=self._seq,
            data=chunk,
            max_total_bytes=self._cap,
        )
        self._seq += 1


@router.post("/v1/files", response_model=None)
async def upload_file(
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    file_store=Depends(get_batch_file_store),
) -> JSONResponse:
    """Upload a JSONL batch input file (``multipart/form-data``: ``file``, ``purpose=batch``)."""
    config = batch_config()
    if not may_use_batch(user_ctx.get("role"), config):
        raise HTTPException(
            403, f"Uploading batch files requires the {config.min_role!r} role or higher"
        )
    row = await store_upload(
        request,
        file_store=file_store,
        owner=_owner(user_ctx),
        check_owner=bool(user_ctx.get("authenticated")),
        config=config,
    )
    return JSONResponse(file_object(row))


async def store_upload(
    request: Request, *, file_store: Any, owner: str, check_owner: bool, config: Any
) -> dict[str, Any]:
    """Stream a ``multipart/form-data`` batch upload into the store; return the ready file row.

    The one upload path, for ``/v1/files`` and the console alike: the body and
    file size limits, the owner's storage cap and hourly upload rate, a size
    and SHA-256 computed as the bytes arrive, and nothing usable left behind
    when anything fails. The caller has checked the role.

    Raises:
        HTTPException: 400 for a malformed upload, 401 when the owner went
            inactive meanwhile, 413 over a size or storage limit, 429 over the
            upload rate.
    """
    try:
        boundary = boundary_of(request.headers.get("content-type"))
    except MultipartError:
        raise HTTPException(
            400, "Content-Type must be multipart/form-data with a boundary"
        ) from None
    if declared_length_exceeds(request, config.max_body_bytes + _MULTIPART_OVERHEAD):
        raise HTTPException(
            413,
            f"The file exceeds the batch size limit of {config.max_body_bytes // (1024 * 1024)} MiB",
        )
    try:
        file_id = await file_store.create_upload(
            user_id=owner,
            filename="upload.jsonl",
            purpose="batch",
            max_total_bytes=config.files_max_total_bytes,
            max_uploads_per_hour=config.file_uploads_per_hour,
        )
    except BatchFileQuotaExceeded as exc:
        raise _quota_error(exc.kind, exc.limit) from None
    upload = _Upload(
        file_store,
        file_id=file_id,
        owner=owner,
        limit=config.max_body_bytes,
        cap=config.files_max_total_bytes,
    )
    fields: dict[str, str] = {}

    async def on_field(name: str, value: str) -> None:
        if name != "purpose":
            raise MultipartError(f"unexpected form field {name[:40]!r}")
        fields[name] = value

    try:
        await read_multipart(
            limited(
                request.stream(),
                config.max_body_bytes + _MULTIPART_OVERHEAD,
                lambda: HTTPException(
                    413,
                    "The upload exceeds the batch size limit of "
                    f"{config.max_body_bytes // (1024 * 1024)} MiB",
                ),
            ),
            boundary,
            on_field=on_field,
            on_file_start=upload.start,
            on_file_data=upload.data,
            on_file_end=upload.end,
        )
        if not upload.seen:
            raise HTTPException(400, "file: a file part is required")
        if fields.get("purpose") != "batch":
            raise HTTPException(400, "purpose: only 'batch' files are accepted here")
        if upload.size == 0:
            raise HTTPException(400, "file: the file is empty")
        row = await file_store.finish_upload(
            file_id=file_id,
            user_id=owner,
            sha256=upload.digest.hexdigest(),
            line_count=upload.lines + (0 if upload.ends_with_newline else 1),
            retention_seconds=config.file_retention_days * 86400,
            check_owner=check_owner,
            filename=upload.filename,
        )
    except MultipartError:
        await file_store.abort_upload(file_id)
        raise HTTPException(
            400,
            "Malformed upload: send multipart/form-data with exactly one 'file' part and a "
            "'purpose' field",
        ) from None
    except BatchFileQuotaExceeded as exc:
        await file_store.abort_upload(file_id)
        raise _quota_error(exc.kind, exc.limit) from None
    except BatchFileOwnerUnavailable:
        await file_store.abort_upload(file_id)
        raise HTTPException(401, "Invalid or expired API key") from None
    except BaseException:
        # A client that went away, a store error: nothing usable is left behind.
        await file_store.abort_upload(file_id)
        raise
    logger.info("File %s uploaded: %d bytes", file_id, upload.size, extra={"user_id": owner})
    return row


def _quota_error(kind: str, limit: int) -> HTTPException:
    if kind == "uploads_per_hour":
        return HTTPException(
            429, f"Too many uploads: at most {limit} batch files per hour; try again later"
        )
    return HTTPException(
        413,
        f"Your batch files would exceed {limit // (1024 * 1024)} MiB of storage; "
        "delete files you no longer need",
    )


@router.get("/v1/files", response_model=None)
async def list_files(
    purpose: str | None = Query(None),
    limit: int = Query(20, ge=1, le=10000),
    after: str | None = Query(None),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    user_ctx: dict = Depends(verify_batch_management_identity),
    file_store=Depends(get_batch_file_store),
) -> JSONResponse:
    """List the caller's batch files."""
    rows = await file_store.list_files(
        user_id=_owner(user_ctx),
        limit=limit + 1,
        after=after,
        purpose=purpose,
        ascending=order == "asc",
    )
    data = [file_object(row) for row in rows[:limit]]
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "has_more": len(rows) > limit,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }
    )


@router.get("/v1/files/{file_id}", response_model=None)
async def retrieve_file(
    file_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    file_store=Depends(get_batch_file_store),
    op_store=Depends(get_operational_store),
) -> JSONResponse:
    """Return a file object."""
    row = await file_store.get_file(file_id, user_id=_lookup_scope(user_ctx))
    if row is None or row.get("status") != "ready":
        raise _not_found(file_id)
    await audit_other_owner(
        request,
        op_store,
        user_ctx,
        row["user_id"],
        action="batch.files.read",
        details={"file_id": file_id},
    )
    return JSONResponse(file_object(row))


@router.get("/v1/files/{file_id}/content", response_model=None)
async def file_content(
    file_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> StreamingResponse:
    """Download a file's bytes: an upload as stored, an output rendered from its batch."""
    row = await file_store.get_file(file_id, user_id=_lookup_scope(user_ctx))
    if row is None or row.get("status") != "ready":
        raise _not_found(file_id)
    if row.get("kind") == "upload" and row["user_id"] != _owner(user_ctx):
        # Another owner's upload is their requests, prompts and all; its
        # results and errors are readable either way.
        require_prompts_readable(log_store)
    await audit_other_owner(
        request,
        op_store,
        user_ctx,
        row["user_id"],
        action="batch.files.download",
        details={"file_id": file_id, "kind": row.get("kind")},
    )
    headers = {
        "content-length": str(int(row.get("bytes") or 0)),
        "content-disposition": content_disposition(row.get("filename") or file_id),
    }
    if row.get("sha256"):
        headers["etag"] = f'"{row["sha256"]}"'
    if row.get("kind") == "upload":
        body = stream_upload(store=store, file_store=file_store, file_id=file_id)
    else:
        batch_id = row.get("batch_id")
        if not batch_id:
            raise _not_found(file_id)
        body = _guarded(
            stream_lines(
                store=store,
                batch_id=batch_id,
                kind=row["kind"],
                version=int(row.get("renderer_version") or 1),
                expected=row,
            )
        )
    return jsonl_download(request, body, headers=headers)


async def _guarded(stream: Any) -> Any:
    try:
        async for chunk in stream:
            yield chunk
    except DownloadInterrupted:
        # The batch was erased mid-download: end the response short, so the
        # client sees fewer bytes than Content-Length, never a complete file.
        logger.warning("A batch output download was interrupted by a deletion")
        raise


@router.delete("/v1/files/{file_id}", response_model=None)
async def delete_file(
    file_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    file_store=Depends(get_batch_file_store),
    op_store=Depends(get_operational_store),
) -> JSONResponse:
    """Delete a file. An output file is gone for good; its batch keeps naming it.

    Another owner's file is audited before it is deleted (:func:`audited_change`).
    """
    scope = _lookup_scope(user_ctx)
    owner, kind = _owner(user_ctx), None
    if scope is None:
        # An administrator: whose file it is, read with the delete's own filter
        # (an expired file is deleted too), so the row names its owner.
        found = await file_store.get_file(file_id, include_deleted=True)
        if found is None or found.get("deleted_at") is not None or found.get("status") != "ready":
            raise _not_found(file_id)
        owner, kind = found["user_id"], found.get("kind")
    async with audited_change(
        request,
        op_store,
        user_ctx,
        owner,
        action="batch.files.delete",
        details={"file_id": file_id, "kind": kind},
    ):
        if not await file_store.delete_file(file_id, user_id=scope):
            raise _not_found(file_id)
    return JSONResponse({"id": file_id, "object": "file", "deleted": True})
