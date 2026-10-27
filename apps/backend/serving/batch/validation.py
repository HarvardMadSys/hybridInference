"""Validating the input file of an OpenAI file batch, in the background.

``POST /v1/batches`` with an ``input_file_id`` only checks the envelope and the
file (owner, purpose, ready, size) and answers ``validating``. A worker then
takes the batch's work lease and reads the file a chunk at a time:

- every line must be a JSON object ``{"custom_id", "method": "POST", "url",
  "body"}`` with ``url`` equal to the batch's ``endpoint``, a ``custom_id``
  unique in the file, and a body its target admits
  (:func:`serving.batch.targets.admit_item`);
- every line must name the same model (a file batch runs one model);
- the file holds at most ``BATCH_MAX_REQUESTS`` lines, none longer than
  ``BATCH_MAX_LINE_KB``.

Valid lines are staged as items in chunks, each chunk in its own transaction
and reserved against the owner's queued-request cap, but nothing staged can be
claimed: the batch stays in phase ``validating``. Only when the whole file is
valid does one transaction flip the phase and make the batch runnable. The
first invalid line fails the whole batch: its staged items are removed, it
counts no request, and ``errors`` lists what was wrong, by line number. So no
request of a file with a bad line anywhere -- the last one included -- ever runs.

A validator that dies leaves its lease to run out; the next one deletes what was
staged and starts over (validation is deterministic). A batch cancelled or
expired while it is validated ends ``cancelled``/``expired`` with nothing run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any

from serving.batch.admission import has_invalid_unicode
from serving.batch.targets import OPENAI_TARGET_URLS, AdmissionContext, ItemRejected, admit_item
from serving.servers.auth import StoredKeyRejected, authenticate_stored_key
from serving.storage.batch_store import BatchLimitExceeded
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from serving.batch.config import BatchConfig

logger = get_logger(__name__)

#: Items staged per transaction.
STAGE_CHUNK = 1000
#: Problems recorded before validation stops reading.
MAX_ERRORS = 20
_MAX_CUSTOM_ID_LENGTH = 512


class _Invalid(Exception):
    def __init__(self, code: str, message: str, line: int | None, param: str | None = None):
        super().__init__(message)
        self.entry = {"code": code, "message": message, "param": param, "line": line}


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


async def iter_lines(
    file_store: Any, file_id: str, *, max_line_bytes: int
) -> AsyncIterator[tuple[int, bytes]]:
    """Yield ``(line number, line bytes)`` of an upload, reading it chunk by chunk.

    Raises:
        _Invalid: A line is longer than ``max_line_bytes``.
    """
    buffer = b""
    line_no = 0
    after = -1
    while True:
        chunks = await file_store.read_chunks(file_id, after_seq=after, limit=4)
        if not chunks:
            break
        for seq, data in chunks:
            after = seq
            buffer += data
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line, buffer = buffer[:newline], buffer[newline + 1 :]
                line_no += 1
                if len(line) > max_line_bytes:
                    raise _Invalid(
                        "line_too_long",
                        f"Line {line_no} is longer than {max_line_bytes} bytes.",
                        line_no,
                    )
                yield line_no, line
            if len(buffer) > max_line_bytes:
                raise _Invalid(
                    "line_too_long",
                    f"Line {line_no + 1} is longer than {max_line_bytes} bytes.",
                    line_no + 1,
                )
    if buffer:
        line_no += 1
        yield line_no, buffer


def _parse_line(line_no: int, raw: bytes, endpoint: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict").rstrip("\r")
    except UnicodeDecodeError:
        raise _Invalid("invalid_json_line", f"Line {line_no} is not UTF-8 text.", line_no) from None
    if not text.strip():
        raise _Invalid("empty_line", f"Line {line_no} is empty.", line_no)
    try:
        entry = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise _Invalid("invalid_json_line", f"Line {line_no} is not valid JSON.", line_no) from None
    if not isinstance(entry, dict):
        raise _Invalid("invalid_json_line", f"Line {line_no} is not a JSON object.", line_no)
    if has_invalid_unicode(entry):
        raise _Invalid(
            "invalid_json_line",
            f"Line {line_no} holds a string that is not valid Unicode.",
            line_no,
        )
    custom_id = entry.get("custom_id")
    if (
        not isinstance(custom_id, str)
        or not 0 < len(custom_id) <= _MAX_CUSTOM_ID_LENGTH
        or "\x00" in custom_id
    ):
        raise _Invalid(
            "invalid_request",
            f"Line {line_no}: custom_id must be a string of 1 to {_MAX_CUSTOM_ID_LENGTH} characters.",
            line_no,
            "custom_id",
        )
    if entry.get("method", "POST") != "POST":
        raise _Invalid(
            "invalid_request", f"Line {line_no}: method must be POST.", line_no, "method"
        )
    if entry.get("url") != endpoint:
        raise _Invalid(
            "mismatched_endpoint",
            f"Line {line_no}: url must be the batch's endpoint, {endpoint}.",
            line_no,
            "url",
        )
    if not isinstance(entry.get("body"), dict):
        raise _Invalid(
            "invalid_request", f"Line {line_no}: body must be a JSON object.", line_no, "body"
        )
    return entry


async def validate_file_batch(
    *,
    store: Any,
    file_store: Any,
    batch: dict[str, Any],
    worker_id: str,
    services: Any,
    cfg: BatchConfig,
    lease_seconds: int,
) -> str:
    """Validate one leased file batch; return how it ended.

    Returns:
        ``published``, ``failed`` (the input was invalid), ``aborted`` (the
        batch was cancelled, expired or deleted, or the lease lost) or
        ``retry`` (a transient problem; the lease runs out and it is retried).
    """
    batch_id = batch["id"]
    if not await store.reset_staged_items(batch_id, worker_id):
        return "aborted"
    upload = await file_store.get_file(batch["input_file_id"], include_deleted=True)
    if upload is None or upload.get("status") != "ready":
        await store.fail_validation(
            batch_id=batch_id,
            worker_id=worker_id,
            errors=_errors(
                [
                    {
                        "code": "invalid_file",
                        "message": "The input file is gone.",
                        "param": "input_file_id",
                        "line": None,
                    }
                ]
            ),
        )
        return "failed"
    try:
        user_ctx = await authenticate_stored_key(
            batch.get("key_hash"), getattr(services, "operational_store", None), check_quota=False
        )
    except StoredKeyRejected as exc:
        await store.fail_validation(
            batch_id=batch_id,
            worker_id=worker_id,
            errors=_errors(
                [
                    {
                        "code": "owner_unauthorized",
                        "message": f"The batch's API key no longer authenticates its owner: {exc.reason}.",
                        "param": None,
                        "line": None,
                    }
                ]
            ),
        )
        return "failed"
    except Exception:
        logger.warning(
            "batch %s: owner could not be authenticated for validation", batch_id, exc_info=True
        )
        return "retry"

    endpoint = batch["endpoint"]
    target = OPENAI_TARGET_URLS.get(endpoint)
    router = getattr(services, "router", None)
    ctx = AdmissionContext(
        user_ctx=user_ctx,
        routes=getattr(router, "routes", {}) if router is not None else {},
        services=services,
        visibility_resolver=getattr(services, "model_visibility_resolver", None),
        embedding_adapters=getattr(services, "embedding_adapters", None),
        embedding_routes=getattr(router, "embedding_routes", None),
    )
    errors: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    model: str | None = None
    pending: list[dict[str, Any]] = []
    count = 0

    async def flush() -> bool:
        nonlocal pending
        if not pending:
            return True
        staged = await store.stage_items(
            batch_id=batch_id,
            worker_id=worker_id,
            user_id=batch["user_id"],
            items=pending,
            max_queued=cfg.max_queued_per_user,
        )
        pending = []
        return staged

    try:
        async for line_no, raw in iter_lines(
            file_store, upload["id"], max_line_bytes=cfg.max_line_bytes
        ):
            if line_no % 500 == 0:
                await asyncio.sleep(0)
            try:
                if target is None:
                    raise _Invalid(
                        "invalid_request", f"Unsupported endpoint {endpoint}.", None, "endpoint"
                    )
                entry = _parse_line(line_no, raw, endpoint)
                digest = hashlib.sha1(entry["custom_id"].encode("utf-8")).digest()
                if digest in seen:
                    raise _Invalid(
                        "duplicate_custom_id",
                        f"Line {line_no}: custom_id {entry['custom_id']!r} appears more than once.",
                        line_no,
                        "custom_id",
                    )
                seen.add(digest)
                count += 1
                if count > cfg.max_requests:
                    raise _Invalid(
                        "too_many_requests",
                        f"The file holds more than {cfg.max_requests} requests.",
                        line_no,
                    )
                try:
                    canonical = await admit_item(target, entry["body"], ctx)
                except ItemRejected as exc:
                    raise _Invalid(
                        exc.code, f"Line {line_no}: {exc.message}", line_no, exc.param
                    ) from None
                if model is None:
                    model = canonical
                elif canonical != model:
                    raise _Invalid(
                        "mismatched_model",
                        f"Line {line_no}: every request of a file batch must use the same model ({model}).",
                        line_no,
                        "body.model",
                    )
            except _Invalid as exc:
                errors.append(exc.entry)
                if len(errors) >= MAX_ERRORS:
                    break
                continue
            if errors:
                continue
            pending.append(
                {
                    "custom_id": entry["custom_id"],
                    "model_id": canonical,
                    "request": entry["body"],
                    "line_no": line_no,
                }
            )
            if len(pending) >= STAGE_CHUNK and not await flush():
                return await _abort(store, batch_id, worker_id)
    except _Invalid as exc:
        errors.append(exc.entry)
    except UnicodeDecodeError:
        errors.append(
            {
                "code": "invalid_file",
                "message": "The file is not UTF-8 text.",
                "param": None,
                "line": None,
            }
        )
    except BatchLimitExceeded as exc:
        errors.append(
            {
                "code": "queued_request_limit",
                "message": f"This batch would take your unfinished batch requests past {exc.limit}.",
                "param": None,
                "line": None,
            }
        )

    if not errors and count == 0:
        errors.append(
            {
                "code": "empty_file",
                "message": "The input file holds no requests.",
                "param": None,
                "line": None,
            }
        )
    if not errors:
        try:
            if not await flush():
                return await _abort(store, batch_id, worker_id)
        except BatchLimitExceeded as exc:
            errors.append(
                {
                    "code": "queued_request_limit",
                    "message": f"This batch would take your unfinished batch requests past {exc.limit}.",
                    "param": None,
                    "line": None,
                }
            )
    if errors:
        await store.fail_validation(batch_id=batch_id, worker_id=worker_id, errors=_errors(errors))
        logger.info(
            "batch %s: input file invalid (%d problem(s)); nothing will run",
            batch_id,
            len(errors),
            extra={"event": "batch_validation_failed"},
        )
        return "failed"
    published = await store.publish_validated(
        batch_id=batch_id, worker_id=worker_id, model_ids=[model] if model else []
    )
    if published is None:
        return await _abort(store, batch_id, worker_id)
    logger.info(
        "batch %s validated: %d requests for %s",
        batch_id,
        count,
        model,
        extra={"event": "batch_validated"},
    )
    return "published"


async def _abort(store: Any, batch_id: str, worker_id: str) -> str:
    """The batch moved on (cancelled, expired, deleted) or the lease was lost."""
    status = await store.validation_status(batch_id, worker_id)
    if status is not None and status != "validating":
        await store.fail_validation(batch_id=batch_id, worker_id=worker_id, errors=_errors([]))
    return "aborted"


def _errors(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"object": "list", "data": entries}
