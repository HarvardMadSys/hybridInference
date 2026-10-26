"""Batch inference surface (``/v1/batches``).

A caller submits many chat-completion requests in one call, polls the batch, then
pages through its results. The create body is inline (``serving.batch.schemas``).
Running the requests is the in-process worker's job (``serving.batch.scheduler``):
it drains them through the ordinary chat-completions handler, re-authenticating
the owner's key before each one, so a stored batch never outlives the access its
owner had.

This router owns admission and the HTTP shapes:

- the surface exists only with a batch store and ``BATCH_ENABLED`` on; otherwise
  every route answers 404;
- creating needs the role ``BATCH_MIN_ROLE`` (``internal`` by default). Reading,
  cancelling and deleting one's own batches do not, so an owner whose role was
  lowered can still collect and erase what they submitted;
- a create body is read under the ``BATCH_MAX_BODY_MB`` cap and every request in
  it is validated before anything is stored: its shape, its chat body, and that
  the caller may use its model;
- a batch belongs to its owner; an administrator may read, cancel and delete
  anyone's. Listing shows the caller's own.

Design record: ``docs/agents/specs/2026-10-25-batch-inference-design.md``.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from routing.model_selection import AUTO_MODEL_ID, AUTO_MODEL_PREFIX, is_auto_model_id
from serving.auto_mode import auto_mode_for
from serving.batch.config import batch_config, may_use_batch
from serving.batch.schemas import (
    CHAT_COMPLETIONS_URL,
    MAX_CUSTOM_ID_LENGTH,
    RESULT_ID_PREFIX,
    BatchCreateRequest,
    BatchDeleted,
    BatchList,
    BatchObject,
    BatchResult,
    BatchResultResponse,
    BatchResultsPage,
)
from serving.batch.view import batch_object
from serving.config.settings import has_role
from serving.model_access import model_access_denial, route_canonical_model_id
from serving.schemas import ChatCompletionRequest
from serving.servers.auth import verify_api_key
from serving.servers.deps import (
    get_batch_store,
    get_model_visibility_resolver,
    get_router,
    get_services,
)
from serving.servers.model_availability import model_not_found_detail
from serving.storage.batch_store import BatchDeleteInProgress, BatchOwnerUnavailable
from serving.utils.logging import get_logger
from serving.utils.webhook_target import webhook_url_problem

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


router = APIRouter(dependencies=[Depends(_require_surface)])


def _reject_on_behalf_of(request: Request) -> None:
    """Refuse ``X-On-Behalf-Of``.

    Every request of a batch runs later under the key that created it, which is
    re-checked before each one. A service key acting for an end user would
    store a batch owned by one identity and authorised by another.
    """
    if request.headers.get("x-on-behalf-of"):
        raise HTTPException(
            400,
            "X-On-Behalf-Of is not supported on /v1/batches: "
            "a batch belongs to the key that creates it",
        )


def _owner(user_ctx: dict[str, Any]) -> str:
    """Return the caller's user id; anonymous callers share one stable identity."""
    return user_ctx.get("user_id") or "anonymous"


def _lookup_scope(user_ctx: dict[str, Any]) -> str | None:
    """Return the owner a lookup is restricted to: the caller, or none for an admin."""
    return None if has_role(user_ctx.get("role") or "free", "admin") else _owner(user_ctx)


def _not_found(batch_id: str) -> HTTPException:
    return HTTPException(404, f"Batch '{batch_id}' not found")


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


async def _read_body(request: Request, limit: int) -> bytearray:
    """Read the request body, refusing it with 413 once it passes ``limit`` bytes.

    A declared ``Content-Length`` over the limit is refused before anything is
    read; otherwise the stream is counted as it arrives, which also bounds a body
    sent without a length.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _too_large(limit)
    return body


def _too_large(limit: int) -> HTTPException:
    return HTTPException(
        413,
        f"Request body exceeds the batch size limit of {limit // (1024 * 1024)} MiB; "
        "split the requests into several batches",
    )


def _reject_json_constant(name: str) -> Any:
    # ``NaN`` and ``Infinity`` are not JSON (RFC 8259), and Postgres would refuse
    # to store a request containing one.
    raise ValueError(f"{name} is not valid JSON")


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


def _parse_create_body(raw: bytes | bytearray) -> BatchCreateRequest:
    """Decode and validate the create body, parsing the JSON once."""
    try:
        decoded = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, "Request body is not valid JSON") from exc
    try:
        return BatchCreateRequest.model_validate(decoded)
    except ValidationError as exc:
        raise HTTPException(400, _envelope_error(exc, decoded)) from None


async def _admit_model(
    model: str,
    label: str,
    *,
    user_ctx: dict[str, Any],
    routes: Any,
    services: Any,
    visibility_resolver: Any,
) -> str:
    """Return the canonical id of ``model`` if the caller may batch it, else answer 400.

    Every reason the caller may not use a model reads as an unknown model, as on
    the chat surface (``serving.model_access``). The canonical id, not the name
    the caller used, is what the per-model batch caps and the worker key on.
    """
    if is_auto_model_id(model) and (
        model not in routes or await auto_mode_for(model, services) is not None
    ):
        raise HTTPException(
            400,
            f"{label}: auto models ({AUTO_MODEL_ID!r}, '{AUTO_MODEL_PREFIX}<tier>') "
            "cannot be used in a batch; name a specific model",
        )
    route = routes.get(model)
    if route is None or (
        await model_access_denial(route, model, user_ctx, visibility_resolver=visibility_resolver)
        is not None
    ):
        raise HTTPException(400, f"{label}: {model_not_found_detail(model, user_ctx=user_ctx)}")
    return route_canonical_model_id(route, model)


async def _validate_requests(
    payload: BatchCreateRequest,
    *,
    config: BatchConfig,
    user_ctx: dict[str, Any],
    routes: Any,
    services: Any,
    visibility_resolver: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Check every request of the body; return the store's items and model ids.

    The first problem answers 400 and names the request it was found in. Each
    item keeps the caller's body exactly as sent for later retrieval, beside the
    canonical id the worker pins into a copy of the body when it dispatches it.
    """
    if len(payload.requests) > config.max_requests:
        raise HTTPException(
            400,
            f"A batch may hold at most {config.max_requests} requests; "
            f"this one has {len(payload.requests)}",
        )
    seen: set[str] = set()
    canonical_ids: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    for index, entry in enumerate(payload.requests):
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
        if entry.url != CHAT_COMPLETIONS_URL:
            raise HTTPException(
                400, f"{label}: url must be {CHAT_COMPLETIONS_URL}, the only endpoint batches serve"
            )
        try:
            chat = ChatCompletionRequest.model_validate(entry.body)
        except ValidationError as exc:
            raise HTTPException(
                400, f"{label}: body is not a valid chat completion request: {_describe(exc)}"
            ) from None
        if chat.stream:
            raise HTTPException(400, f"{label}: stream must not be true in a batch")
        canonical = canonical_ids.get(chat.model)
        if canonical is None:
            canonical = await _admit_model(
                chat.model,
                label,
                user_ctx=user_ctx,
                routes=routes,
                services=services,
                visibility_resolver=visibility_resolver,
            )
            canonical_ids[chat.model] = canonical
        items.append({"custom_id": entry.custom_id, "model_id": canonical, "request": entry.body})
    return items, sorted(set(canonical_ids.values()))


async def _check_active_limit(store: Any, owner: str, config: BatchConfig) -> None:
    active = await store.count_active_batches(owner)
    if active >= config.max_active_per_user:
        raise HTTPException(
            429,
            f"Too many unfinished batches: at most {config.max_active_per_user} may be "
            "queued or running at once; wait for one to finish or cancel one",
        )


def _wake_worker(services: Any) -> None:
    """Tell this process's worker a batch is waiting, so it need not wait for its next poll."""
    scheduler = getattr(services, "batch_scheduler", None)
    if scheduler is None:
        return
    try:
        scheduler.notify()
    except Exception:
        # The batch is stored either way and the worker polls; failing the create
        # now would only invite the caller to submit it twice.
        logger.warning("Could not wake the batch worker", exc_info=True)


@router.post("/v1/batches", response_model=BatchObject)
async def create_batch(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    store=Depends(get_batch_store),
    services=Depends(get_services),
    router_exec=Depends(get_router),
    visibility_resolver=Depends(get_model_visibility_resolver),
) -> BatchObject:
    """Create a batch from an inline array of chat-completion requests."""
    store, config = _surface(store)
    _reject_on_behalf_of(request)
    if not may_use_batch(user_ctx.get("role"), config):
        raise HTTPException(
            403, f"Creating batches requires the {config.min_role!r} role or higher"
        )
    owner = _owner(user_ctx)
    # Checked before the body is read, so a caller at the limit is not made to
    # upload it, and again before the insert, since validating a large body
    # takes a while.
    await _check_active_limit(store, owner, config)
    payload = _parse_create_body(await _read_body(request, config.max_body_bytes))
    if payload.webhook_url is not None:
        problem = webhook_url_problem(payload.webhook_url)
        if problem is not None:
            raise HTTPException(400, f"webhook_url {problem}")
    items, model_ids = await _validate_requests(
        payload,
        config=config,
        user_ctx=user_ctx,
        routes=router_exec.routes,
        services=services,
        visibility_resolver=visibility_resolver,
    )
    await _check_active_limit(store, owner, config)
    try:
        row = await store.create_batch(
            batch_id=f"batch_{uuid.uuid4().hex}",
            user_id=owner,
            key_hash=user_ctx.get("auth_key_hash"),
            endpoint=CHAT_COMPLETIONS_URL,
            model_ids=model_ids,
            items=items,
            metadata=payload.metadata,
            completion_window_seconds=config.completion_window_seconds,
            webhook_url=payload.webhook_url,
        )
    except BatchOwnerUnavailable:
        # Authentication succeeded before the body was validated, but the
        # account may have been suspended or hard-deleted before this
        # transaction locked it. Present the same answer as the auth path.
        raise HTTPException(401, "Invalid or expired API key") from None
    except ValueError as exc:
        # The store refuses what it cannot hold (a repeated or unstorable
        # custom_id). The checks above catch those first; this keeps a 400, with
        # static text, since a response never carries exception text.
        logger.warning("Batch store refused a batch: %s", exc, extra={"user_id": owner})
        raise HTTPException(
            400, "Invalid batch: a custom_id is repeated or holds a character that cannot be stored"
        ) from None
    logger.info(
        "Batch %s created: %d requests for %s",
        row["id"],
        len(items),
        ", ".join(model_ids),
        extra={"user_id": owner},
    )
    _wake_worker(services)
    return batch_object(row, config)


@router.get("/v1/batches", response_model=BatchList)
async def list_batches(
    request: Request,
    after: str | None = Query(None, description="Id of the last batch of the previous page"),
    limit: int = Query(20, ge=1, le=100),
    user_ctx: dict = Depends(verify_api_key),
    store=Depends(get_batch_store),
) -> BatchList:
    """List the caller's batches, newest first."""
    store, config = _surface(store)
    _reject_on_behalf_of(request)
    rows = await store.list_batches(user_id=_owner(user_ctx), limit=limit + 1, after=after)
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
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    store=Depends(get_batch_store),
) -> BatchObject:
    """Return a batch: its status and request counts."""
    store, config = _surface(store)
    _reject_on_behalf_of(request)
    row = await store.get_batch(batch_id, user_id=_lookup_scope(user_ctx))
    if row is None:
        raise _not_found(batch_id)
    return batch_object(row, config)


@router.post("/v1/batches/{batch_id}/cancel", response_model=BatchObject)
async def cancel_batch(
    batch_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    store=Depends(get_batch_store),
) -> BatchObject:
    """Stop a batch: queued requests are cancelled, running ones finish.

    The batch is ``cancelling`` until its running requests are done, then
    ``cancelled``. A batch that already finished is returned unchanged.
    """
    store, config = _surface(store)
    _reject_on_behalf_of(request)
    row = await store.cancel_batch(batch_id, user_id=_lookup_scope(user_ctx))
    if row is None:
        raise _not_found(batch_id)
    return batch_object(row, config)


@router.delete("/v1/batches/{batch_id}", response_model=BatchDeleted)
async def delete_batch(
    batch_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    store=Depends(get_batch_store),
) -> BatchDeleted:
    """Delete a batch unless one of its requests still holds a live lease."""
    store, _config = _surface(store)
    _reject_on_behalf_of(request)
    try:
        deleted = await store.delete_batch(batch_id, user_id=_lookup_scope(user_ctx))
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
    request: Request,
    after: str | None = Query(None, description="Id of the last result of the previous page"),
    limit: int = Query(100, ge=1, le=1000),
    user_ctx: dict = Depends(verify_api_key),
    store=Depends(get_batch_store),
) -> BatchResultsPage:
    """Page through a batch's requests in submission order, with their outcomes."""
    store, _config = _surface(store)
    _reject_on_behalf_of(request)
    cursor = _results_cursor(after)
    if await store.get_batch(batch_id, user_id=_lookup_scope(user_ctx)) is None:
        raise _not_found(batch_id)
    rows = await store.list_results(batch_id, after=cursor, limit=limit + 1)
    page = [_result(row) for row in rows[:limit]]
    return BatchResultsPage(
        data=page,
        has_more=len(rows) > limit,
        first_id=page[0].id if page else None,
        last_id=page[-1].id if page else None,
    )
