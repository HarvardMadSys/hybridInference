"""OpenAI-shaped batch surface (``/v1/batches``).

A caller submits many requests in one call, polls the batch, then collects its
results. Two ways in, exclusive of each other on ``POST /v1/batches``:

- **inline**: a ``requests`` array (``custom_id``, ``method``, ``url``,
  ``body``). Results are paged (``GET /v1/batches/{id}/results``) or exported
  as JSONL once the batch has finished (``GET /v1/batches/{id}/output.jsonl``).
  Every request of an inline batch has the same ``url`` (one target per batch);
  chat batches may mix models;
- **file**: OpenAI's shape, an uploaded JSONL file (``/v1/files``,
  ``purpose=batch``) named by ``input_file_id`` with its ``endpoint`` and the
  ``24h`` ``completion_window``. The file is validated in the background
  (status ``validating``); one bad line fails the batch before anything runs.
  Results arrive as ``output_file_id`` / ``error_file_id``.

Targets: ``/v1/chat/completions``, ``/v1/embeddings`` and ``/v1/responses``
(independent, unstored requests only), inline or from a file, whenever
``BATCH_ENABLED`` is on. Running the requests is the in-process
worker's job (``serving.batch.scheduler``): it re-authenticates the owner's key
before each one, so a stored batch never outlives the access its owner had.

This router owns admission and the HTTP shapes:

- the surface exists only with a batch store and ``BATCH_ENABLED`` on; otherwise
  every route answers 404;
- every route authenticates the key and account fully, but only creating is
  held to the daily cost quota: reading, downloading, cancelling and deleting
  keep working when the quota is spent (``verify_batch_management_identity``);
- creating needs the role ``BATCH_MIN_ROLE`` (``internal`` by default);
  ``Idempotency-Key`` replays are answered before that, the quota and the caps
  (``serving.batch.admission``);
- a batch belongs to its owner, whichever of the owner's keys asks; an
  administrator may read, cancel and delete anyone's. Listing shows the
  caller's own OpenAI-shaped batches; an Anthropic Message Batch is not found
  here, as an OpenAI batch is not found on the Anthropic routes.

Design records: ``docs/agents/specs/2026-10-25-batch-inference-design.md``,
``docs/agents/specs/2026-10-26-batch-api-expansion-design.zh.md`` and
``docs/agents/specs/2026-10-27-batch-default-availability-amendment.zh.md``.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from serving.batch import admission
from serving.batch.config import PROTOCOL_WINDOW_SECONDS, batch_config, may_use_batch
from serving.batch.delivery import stream_lines
from serving.batch.renderers import openai_status
from serving.batch.schemas import (
    CHAT_COMPLETIONS_URL,
    FILE_COMPLETION_WINDOW,
    MAX_CUSTOM_ID_LENGTH,
    RESULT_ID_PREFIX,
    BatchCreateRequest,
    BatchDeleted,
    BatchList,
    BatchObject,
    BatchRequestCounts,
    BatchResult,
    BatchResultResponse,
    BatchResultsPage,
    BatchUsage,
)
from serving.batch.targets import OPENAI_TARGET_URLS, AdmissionContext, check_item
from serving.config.settings import has_role
from serving.servers.auth import enforce_daily_quota, verify_batch_management_identity
from serving.servers.deps import (
    get_batch_file_store,
    get_batch_store,
    get_model_visibility_resolver,
    get_router,
    get_services,
)
from serving.servers.routers.files import content_disposition
from serving.storage.batch_store import (
    OPENAI_SURFACES,
    PHASE_SEALED,
    PHASE_VALIDATING,
    SURFACE_OPENAI_FILE,
    SURFACE_OPENAI_INLINE,
    TERMINAL_BATCH_STATUSES,
    BatchDeleteInProgress,
)
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.batch.config import BatchConfig

logger = get_logger(__name__)

_UNAVAILABLE = "Batch inference is not available on this gateway"

# A results cursor: the id of the last result of the previous page. Eighteen
# digits keep it inside the BIGINT it is compared with.
_RESULT_ID = re.compile(rf"{RESULT_ID_PREFIX}(\d{{1,18}})")

# Requests validated between two yields to the event loop.
_YIELD_EVERY = 1000


def _surface(store: Any) -> tuple[Any, BatchConfig]:
    """Return the store and the settings in force, or answer 404 when batches are off."""
    config = batch_config()
    if store is None or not config.enabled:
        raise HTTPException(404, _UNAVAILABLE)
    return store, config


def _require_surface(store: Any = Depends(get_batch_store)) -> None:
    """Hide the whole surface before authentication or request validation runs."""
    _surface(store)


def _reject_on_behalf_of(request: Request) -> None:
    """Refuse ``X-On-Behalf-Of`` (the identity dependency does too; this holds without it).

    Work stored here runs later under the key that created it, re-checked
    before each request. A service key acting for an end user would store work
    owned by one identity and authorised by another.
    """
    if request.headers.get("x-on-behalf-of"):
        raise HTTPException(
            400,
            "X-On-Behalf-Of is not supported on /v1/batches: "
            "a batch belongs to the key that creates it",
        )


router = APIRouter(dependencies=[Depends(_require_surface), Depends(_reject_on_behalf_of)])


def _owner(user_ctx: dict[str, Any]) -> str:
    """Return the caller's user id; anonymous callers share one stable identity."""
    return user_ctx.get("user_id") or "anonymous"


def _lookup_scope(user_ctx: dict[str, Any]) -> str | None:
    """Return the owner a lookup is restricted to: the caller, or none for an admin."""
    return None if has_role(user_ctx.get("role") or "free", "admin") else _owner(user_ctx)


def _not_found(batch_id: str) -> HTTPException:
    return HTTPException(404, f"Batch '{batch_id}' not found")


def _epoch(value: Any) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None


def _completion_window(row: dict[str, Any], config: BatchConfig) -> str:
    """Return the window the batch was created with, as OpenAI spells it (``24h``).

    Read off the batch's own timestamps rather than the live setting, which may
    have changed since.
    """
    created, expires = row.get("created_at"), row.get("expires_at")
    if isinstance(created, datetime) and isinstance(expires, datetime):
        hours = max(1, round((expires - created).total_seconds() / 3600))
    else:
        hours = config.completion_window_hours
    return f"{hours}h"


def batch_object(row: dict[str, Any], config: BatchConfig) -> BatchObject:
    """Map a store row to the API object.

    Field by field, so the row's ownership columns (``user_id``, ``key_hash``)
    can never reach a response.
    """
    status = openai_status(row)
    sealed = row.get("phase") == PHASE_SEALED or row.get("api_surface") != SURFACE_OPENAI_FILE
    prompt = int(row.get("prompt_tokens") or 0)
    completion = int(row.get("completion_tokens") or 0)
    return BatchObject(
        id=row["id"],
        endpoint=row.get("endpoint") or CHAT_COMPLETIONS_URL,
        errors=row.get("validation_error") if status == "failed" else None,
        input_file_id=row.get("input_file_id"),
        completion_window=_completion_window(row, config),
        status=status,
        output_file_id=row.get("output_file_id") if sealed else None,
        error_file_id=row.get("error_file_id") if sealed else None,
        created_at=_epoch(row.get("created_at")) or 0,
        in_progress_at=_epoch(row.get("in_progress_at")),
        expires_at=_epoch(row.get("expires_at")),
        finalizing_at=_epoch(row.get("finalizing_at")),
        completed_at=_epoch(row.get("completed_at")) if status == "completed" else None,
        failed_at=_epoch(row.get("failed_at")) if status == "failed" else None,
        expired_at=_epoch(row.get("expired_at")) if status == "expired" else None,
        cancelling_at=_epoch(row.get("cancelling_at")),
        cancelled_at=_epoch(row.get("cancelled_at")) if status == "cancelled" else None,
        request_counts=BatchRequestCounts(
            total=row.get("request_count") or 0,
            completed=row.get("completed_count") or 0,
            failed=row.get("failed_count") or 0,
        ),
        metadata=row.get("metadata"),
        usage=BatchUsage(
            input_tokens=prompt, output_tokens=completion, total_tokens=prompt + completion
        ),
        cost_usd=float(row.get("cost_usd") or 0.0),
        cost_unknown_attempts=int(row.get("unknown_cost_count") or 0),
        results_expires_at=_epoch(row.get("results_expires_at")),
        error=row.get("error"),
    )


def _result(row: dict[str, Any]) -> BatchResult:
    """Map a stored request to a result, shaped like an OpenAI batch output line."""
    response = None
    if row.get("response") is not None:
        response = BatchResultResponse(
            status_code=row.get("status_code"),
            request_id=row.get("request_id"),
            body=row["response"],
        )
    return BatchResult(
        id=f"{RESULT_ID_PREFIX}{row['id']}",
        custom_id=row["custom_id"],
        status=row["status"],
        response=response,
        error=row.get("error"),
    )


def _results_cursor(after: str | None) -> int | None:
    if after is None:
        return None
    match = _RESULT_ID.fullmatch(after)
    if match is None:
        raise HTTPException(400, f"'after' must be a result id, such as {RESULT_ID_PREFIX}42")
    return int(match.group(1))


def _item_label(index: int, custom_id: Any) -> str:
    """Name a request of the body in an error message."""
    if isinstance(custom_id, str) and 0 < len(custom_id) <= MAX_CUSTOM_ID_LENGTH:
        return f"requests[{index}] (custom_id {custom_id!r})"
    return f"requests[{index}]"


def _describe(exc: ValidationError, *, skip: int = 0) -> str:
    """Summarize a validation failure: its first problem, without the input it rejected.

    The first ``skip`` parts of the problem's location are left out, for a caller
    that names them itself.
    """
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    first = errors[0]
    where = ".".join(str(part) for part in tuple(first.get("loc", ()))[skip:])
    text = f"{where}: {first['msg']}" if where else str(first["msg"])
    if len(errors) > 1:
        text += f" (and {len(errors) - 1} more)"
    return text


def _envelope_error(exc: ValidationError, decoded: Any) -> str:
    """Describe a create body whose envelope is invalid, naming the request at fault."""
    loc = tuple(exc.errors(include_url=False, include_input=False)[0].get("loc", ()))
    if len(loc) >= 2 and loc[0] == "requests" and isinstance(loc[1], int):
        index = loc[1]
        raw_items = decoded.get("requests") if isinstance(decoded, dict) else None
        raw = raw_items[index] if isinstance(raw_items, list) and index < len(raw_items) else None
        custom_id = raw.get("custom_id") if isinstance(raw, dict) else None
        return f"{_item_label(index, custom_id)}: {_describe(exc, skip=2)}"
    return f"Invalid batch request body: {_describe(exc)}"


def _parse_create_body(raw: bytes | bytearray) -> tuple[BatchCreateRequest, Any]:
    """Decode and validate the create body, parsing the JSON once."""
    try:
        decoded = json.loads(raw, parse_constant=admission.reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, "Request body is not valid JSON") from exc
    if admission.has_invalid_unicode(decoded):
        raise HTTPException(400, "Request body holds a string that is not valid Unicode")
    try:
        return BatchCreateRequest.model_validate(decoded), decoded
    except ValidationError as exc:
        raise HTTPException(400, _envelope_error(exc, decoded)) from None


def _admission_context(
    user_ctx: dict[str, Any], *, services: Any, router_exec: Any, visibility_resolver: Any
) -> AdmissionContext:
    return AdmissionContext(
        user_ctx=user_ctx,
        routes=router_exec.routes,
        services=services,
        visibility_resolver=visibility_resolver,
        embedding_adapters=getattr(services, "embedding_adapters", None),
    )


async def _validate_inline(
    payload: BatchCreateRequest,
    *,
    config: BatchConfig,
    ctx: AdmissionContext,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Check every request of an inline body; return its target and the store's items.

    The first problem answers 400 and names the request it was found in. Each
    item keeps the caller's body exactly as sent for later retrieval, beside the
    canonical id the worker pins into a copy of the body when it dispatches it.
    """
    requests = payload.requests or []
    if len(requests) > config.max_requests:
        raise HTTPException(
            400,
            f"A batch may hold at most {config.max_requests} requests; "
            f"this one has {len(requests)}",
        )
    url = requests[0].url
    target = OPENAI_TARGET_URLS.get(url)
    if target is None:
        raise HTTPException(
            400,
            f"{_item_label(0, requests[0].custom_id)}: url must be one of "
            f"{', '.join(sorted(OPENAI_TARGET_URLS))}",
        )
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    models: set[str] = set()
    for index, entry in enumerate(requests):
        if index and index % _YIELD_EVERY == 0:
            # Tens of thousands of validations are a few hundred milliseconds of
            # CPU; let the event loop serve other requests in between.
            await asyncio.sleep(0)
        label = _item_label(index, entry.custom_id)
        if entry.custom_id in seen:
            raise HTTPException(
                400, f"{label}: custom_id is not unique; every request needs its own"
            )
        seen.add(entry.custom_id)
        if entry.url != url:
            raise HTTPException(
                400,
                f"{label}: every request of a batch must use the same url ({url}); "
                "submit one batch per endpoint",
            )
        canonical, problem = await check_item(target, entry.body, ctx)
        if problem is not None or canonical is None:
            raise HTTPException(400, f"{label}: {problem.message if problem else 'refused'}")
        models.add(canonical)
        items.append({"custom_id": entry.custom_id, "model_id": canonical, "request": entry.body})
    return url, items, sorted(models)


async def _check_file(
    file_store: Any, payload: BatchCreateRequest, owner: str, config: BatchConfig
) -> dict[str, Any]:
    """The upload a file batch names, checked: the caller's, for batches, ready, in size."""
    if file_store is None:
        raise HTTPException(404, _UNAVAILABLE)
    if payload.endpoint not in OPENAI_TARGET_URLS:
        raise HTTPException(400, f"endpoint must be one of {', '.join(sorted(OPENAI_TARGET_URLS))}")
    if payload.completion_window != FILE_COMPLETION_WINDOW:
        raise HTTPException(400, f"completion_window must be {FILE_COMPLETION_WINDOW!r}")
    upload = await file_store.get_file(payload.input_file_id, user_id=owner)
    if upload is None or upload.get("kind") != "upload":
        raise HTTPException(400, f"File '{payload.input_file_id}' not found")
    if upload.get("purpose") != "batch":
        raise HTTPException(400, "The input file's purpose must be 'batch'")
    if upload.get("status") != "ready":
        raise HTTPException(400, "The input file is not ready yet")
    if int(upload.get("bytes") or 0) > config.max_body_bytes:
        raise admission.too_large(config.max_body_bytes)
    return upload


def _may_create(request: Request, user_ctx: dict[str, Any], config: BatchConfig) -> None:
    """Refuse a caller below ``BATCH_MIN_ROLE`` (403) or out of daily quota (429)."""
    if not may_use_batch(user_ctx.get("role"), config):
        raise HTTPException(
            403, f"Creating batches requires the {config.min_role!r} role or higher"
        )
    enforce_daily_quota(request, user_ctx)


@router.post("/v1/batches", response_model=BatchObject)
async def create_batch(
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
    services=Depends(get_services),
    router_exec=Depends(get_router),
    visibility_resolver=Depends(get_model_visibility_resolver),
) -> BatchObject:
    """Create a batch from inline requests, or from an uploaded JSONL file."""
    store, config = _surface(store)
    owner = _owner(user_ctx)
    key = admission.idempotency_key(request)
    if key is None:
        # Nothing to replay: refuse a caller who may not create before making
        # them upload the body.
        _may_create(request, user_ctx, config)
        await admission.precheck_active(store, owner, config)
    payload, _decoded = _parse_create_body(
        await admission.read_body(request, config.max_body_bytes)
    )
    file_mode = payload.input_file_id is not None
    surface = SURFACE_OPENAI_FILE if file_mode else SURFACE_OPENAI_INLINE
    if file_mode:
        digest = admission.request_digest(
            surface,
            input_file_id=payload.input_file_id,
            endpoint=payload.endpoint,
            completion_window=payload.completion_window,
            metadata=payload.metadata,
        )
    else:
        digest = admission.request_digest(
            surface,
            requests=[entry.model_dump(mode="json") for entry in payload.requests or []],
            metadata=payload.metadata,
        )
    replay = await admission.find_replay(
        store, owner=owner, surface=surface, key=key, digest=digest
    )
    if replay is not None:
        return batch_object(replay, config)

    _may_create(request, user_ctx, config)
    if key is None:
        # Checked before the requests are validated, so a caller at the limit is
        # not made to wait for it; enforced again, atomically, by the insert.
        # Not with an Idempotency-Key: a concurrent create with the same key may
        # have just filled the cap, and the insert transaction answers that
        # with the first create's batch, not a refusal.
        await admission.precheck_active(store, owner, config)
    common = {
        "batch_id": f"batch_{uuid.uuid4().hex}",
        "user_id": owner,
        "key_hash": user_ctx.get("auth_key_hash"),
        "metadata": payload.metadata,
        "retention_seconds": config.retention_seconds,
        "idempotency": {"key": key, "digest": digest} if key else None,
        "max_active": config.max_active_per_user,
        "max_queued": config.max_queued_per_user,
    }
    if file_mode:
        upload = await _check_file(file_store, payload, owner, config)
        row = await admission.create(
            store,
            **common,
            endpoint=payload.endpoint,
            model_ids=[],
            items=[],
            completion_window_seconds=PROTOCOL_WINDOW_SECONDS,
            api_surface=SURFACE_OPENAI_FILE,
            target=OPENAI_TARGET_URLS[payload.endpoint or ""],
            phase=PHASE_VALIDATING,
            input_file_id=upload["id"],
        )
    else:
        ctx = _admission_context(
            user_ctx,
            services=services,
            router_exec=router_exec,
            visibility_resolver=visibility_resolver,
        )
        url, items, model_ids = await _validate_inline(payload, config=config, ctx=ctx)
        row = await admission.create(
            store,
            **common,
            endpoint=url,
            model_ids=model_ids,
            items=items,
            completion_window_seconds=config.completion_window_seconds,
            api_surface=SURFACE_OPENAI_INLINE,
            target=OPENAI_TARGET_URLS[url],
        )
    if not row.get("replayed"):
        logger.info(
            "Batch %s created (%s, %s): %d requests",
            row["id"],
            surface,
            row.get("endpoint"),
            int(row.get("request_count") or 0),
            extra={"user_id": owner},
        )
    admission.wake_worker(services)
    return batch_object(row, config)


@router.get("/v1/batches", response_model=BatchList)
async def list_batches(
    after: str | None = Query(None, description="Id of the last batch of the previous page"),
    limit: int = Query(20, ge=1, le=100),
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> BatchList:
    """List the caller's OpenAI-shaped batches, newest first."""
    store, config = _surface(store)
    rows = await store.list_batches(
        user_id=_owner(user_ctx), limit=limit + 1, after=after, surfaces=OPENAI_SURFACES
    )
    page = [batch_object(row, config) for row in rows[:limit]]
    return BatchList(
        data=page,
        has_more=len(rows) > limit,
        first_id=page[0].id if page else None,
        last_id=page[-1].id if page else None,
    )


@router.get("/v1/batches/{batch_id}", response_model=BatchObject)
async def retrieve_batch(
    batch_id: str,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> BatchObject:
    """Return a batch: its status and request counts."""
    store, config = _surface(store)
    row = await store.get_batch(batch_id, user_id=_lookup_scope(user_ctx), surfaces=OPENAI_SURFACES)
    if row is None:
        raise _not_found(batch_id)
    return batch_object(row, config)


@router.post("/v1/batches/{batch_id}/cancel", response_model=BatchObject)
async def cancel_batch(
    batch_id: str,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> BatchObject:
    """Stop a batch: queued requests are cancelled, running ones finish.

    The batch is ``cancelling`` until its running requests are done, then
    ``cancelled``. A batch that already finished, or is sealing its results, is
    returned unchanged.
    """
    store, config = _surface(store)
    row = await store.cancel_batch(
        batch_id, user_id=_lookup_scope(user_ctx), surfaces=OPENAI_SURFACES
    )
    if row is None:
        raise _not_found(batch_id)
    return batch_object(row, config)


@router.delete("/v1/batches/{batch_id}", response_model=BatchDeleted)
async def delete_batch(
    batch_id: str,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> BatchDeleted:
    """Delete a batch unless one of its requests still holds a live lease."""
    store, _config = _surface(store)
    try:
        deleted = await store.delete_batch(
            batch_id, user_id=_lookup_scope(user_ctx), surfaces=OPENAI_SURFACES
        )
    except BatchDeleteInProgress:
        raise HTTPException(
            409,
            f"Batch '{batch_id}' has a running request; cancel the batch and wait for its "
            "running requests to finish before deleting it",
        ) from None
    if not deleted:
        raise _not_found(batch_id)
    return BatchDeleted(id=batch_id)


@router.get("/v1/batches/{batch_id}/results", response_model=BatchResultsPage)
async def list_batch_results(
    batch_id: str,
    after: str | None = Query(None, description="Id of the last result of the previous page"),
    limit: int = Query(100, ge=1, le=1000),
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> BatchResultsPage:
    """Page through a batch's requests in submission order, with their outcomes."""
    store, _config = _surface(store)
    cursor = _results_cursor(after)
    if (
        await store.get_batch(batch_id, user_id=_lookup_scope(user_ctx), surfaces=OPENAI_SURFACES)
        is None
    ):
        raise _not_found(batch_id)
    rows = await store.list_results(batch_id, after=cursor, limit=limit + 1)
    page = [_result(row) for row in rows[:limit]]
    return BatchResultsPage(
        data=page,
        has_more=len(rows) > limit,
        first_id=page[0].id if page else None,
        last_id=page[-1].id if page else None,
    )


def results_final(row: dict[str, Any]) -> bool:
    """Whether a batch's results are fixed (sealed; or final, for a batch from before phases)."""
    if row.get("phase") == PHASE_SEALED:
        return True
    return int(row.get("schema_version") or 1) < 2 and row.get("status") in TERMINAL_BATCH_STATUSES


@router.get("/v1/batches/{batch_id}/output.jsonl", response_model=None)
async def export_batch_results(
    batch_id: str,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> StreamingResponse:
    """Download every result of a finished batch as JSONL (OpenAI output-line shape)."""
    store, _config = _surface(store)
    row = await store.get_batch(batch_id, user_id=_lookup_scope(user_ctx), surfaces=OPENAI_SURFACES)
    if row is None:
        raise _not_found(batch_id)
    if not results_final(row):
        raise HTTPException(409, f"Batch '{batch_id}' has not finished; its results are not final")
    return StreamingResponse(
        stream_lines(store=store, batch_id=batch_id, surface=row["api_surface"], kind="all"),
        media_type="application/jsonl",
        headers={"content-disposition": content_disposition(f"{batch_id}.jsonl")},
    )
