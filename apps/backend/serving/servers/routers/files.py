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

This is not a general Files API: a ``batch`` file cannot be referenced from a
chat, Messages or Responses request.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from serving.batch.config import batch_config, may_use_batch
from serving.batch.delivery import DownloadInterrupted, stream_lines, stream_upload
from serving.batch.multipart import MultipartError, boundary_of, limited, read_multipart
from serving.config.settings import has_role
from serving.servers.auth import verify_batch_management_identity
from serving.servers.deps import get_batch_file_store, get_batch_store
from serving.storage.batch_file_store import (
    CHUNK_BYTES,
    BatchFileOwnerUnavailable,
    BatchFileQuotaExceeded,
)
from serving.utils.logging import get_logger

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
    return None if has_role(user_ctx.get("role") or "free", "admin") else _owner(user_ctx)


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
    try:
        boundary = boundary_of(request.headers.get("content-type"))
    except MultipartError:
        raise HTTPException(
            400, "Content-Type must be multipart/form-data with a boundary"
        ) from None
    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > config.max_body_bytes + _MULTIPART_OVERHEAD
    ):
        raise HTTPException(
            413,
            f"The file exceeds the batch size limit of {config.max_body_bytes // (1024 * 1024)} MiB",
        )
    owner = _owner(user_ctx)
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
            check_owner=bool(user_ctx.get("authenticated")),
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
    return JSONResponse(file_object(row))


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
    user_ctx: dict = Depends(verify_batch_management_identity),
    file_store=Depends(get_batch_file_store),
) -> JSONResponse:
    """Return a file object."""
    row = await file_store.get_file(file_id, user_id=_lookup_scope(user_ctx))
    if row is None or row.get("status") != "ready":
        raise _not_found(file_id)
    return JSONResponse(file_object(row))


@router.get("/v1/files/{file_id}/content", response_model=None)
async def file_content(
    file_id: str,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
) -> StreamingResponse:
    """Download a file's bytes: an upload as stored, an output rendered from its batch."""
    row = await file_store.get_file(file_id, user_id=_lookup_scope(user_ctx))
    if row is None or row.get("status") != "ready":
        raise _not_found(file_id)
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
                surface="openai_file",
                kind=row["kind"],
                version=int(row.get("renderer_version") or 1),
                expected=row,
            )
        )
    return StreamingResponse(body, media_type="application/jsonl", headers=headers)


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
    user_ctx: dict = Depends(verify_batch_management_identity),
    file_store=Depends(get_batch_file_store),
) -> JSONResponse:
    """Delete a file. An output file is gone for good; its batch keeps naming it."""
    if not await file_store.delete_file(file_id, user_id=_lookup_scope(user_ctx)):
        raise _not_found(file_id)
    return JSONResponse({"id": file_id, "object": "file", "deleted": True})
