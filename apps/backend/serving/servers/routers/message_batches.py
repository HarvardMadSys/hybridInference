"""Anthropic Message Batches (``/v1/messages/batches``).

Served under both prefixes the Messages API uses here, ``/v1/messages/batches``
and ``/anthropic/v1/messages/batches``, with Anthropic's request and response
shapes and error envelope (the path-aware exception handler in
``anthropic_messages`` formats every error on these paths):

- ``POST``: ``{"requests": [{"custom_id", "params"}]}``. The envelope, the
  ``custom_id`` pattern and uniqueness, and the count and size limits are
  checked at once, and any problem refuses the whole batch. Each request's
  ``params`` is then checked on its own (:mod:`serving.batch.targets`) against
  the ``anthropic-version`` the batch is created under
  (:mod:`serving.batch.profiles`): a request that fails -- including one naming
  a model the caller cannot use, which reads exactly like an unknown model --
  becomes an ``errored`` result without touching the others. When every
  request fails, the batch has ended before this answer is sent.
- ``GET`` (list, ``limit``/``before_id``/``after_id``), ``GET {id}``,
  ``POST {id}/cancel``, ``DELETE {id}`` (only once ended) and ``GET
  {id}/results`` (JSONL, only once ended).

The ``anthropic-version`` header is required, and must be one this gateway
knows (``BATCH_ANTHROPIC_VERSIONS`` decides which may create); ``anthropic-beta``
is refused. Each item runs once through the Messages handler; a tool call in a
result is for the client to execute and send back in a new request -- the
gateway never runs user tools. Message Batches belong to the user, as every
batch here does: there are no workspaces, and an administrator may manage
anyone's. Creating needs ``BATCH_MIN_ROLE``; everything else works for an
owner whatever their role and quota.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from serving.batch import admission
from serving.batch.config import (
    KNOWN_ANTHROPIC_VERSIONS,
    PROTOCOL_WINDOW_SECONDS,
    batch_config,
    may_use_batch,
)
from serving.batch.delivery import stream_lines
from serving.batch.profiles import PROFILES
from serving.batch.renderers import anthropic_batch
from serving.batch.targets import AdmissionContext, check_item
from serving.config.settings import has_role
from serving.servers.auth import enforce_daily_quota, verify_batch_management_identity
from serving.servers.deps import (
    get_batch_store,
    get_model_visibility_resolver,
    get_router,
    get_services,
)
from serving.storage.batch_store import (
    PHASE_SEALED,
    SURFACE_ANTHROPIC,
    TARGET_MESSAGES,
    BatchDeleteInProgress,
    BatchNotEnded,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)

_SURFACES = frozenset({SURFACE_ANTHROPIC})
_CUSTOM_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
#: Anthropic's own ceiling on requests per batch; BATCH_MAX_REQUESTS may be lower.
_PROTOCOL_MAX_REQUESTS = 100_000
_UNAVAILABLE = "Message Batches are not available on this gateway"


def _require_surface(store: Any = Depends(get_batch_store)) -> None:
    """Hide the whole surface (404) without a batch store or with batches off."""
    if store is None or not batch_config().enabled:
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
            "X-On-Behalf-Of is not supported for Message Batches: "
            "a batch belongs to the key that creates it",
        )


router = APIRouter(dependencies=[Depends(_require_surface), Depends(_reject_on_behalf_of)])


def _owner(user_ctx: dict[str, Any]) -> str:
    return user_ctx.get("user_id") or "anonymous"


def _lookup_scope(user_ctx: dict[str, Any]) -> str | None:
    return None if has_role(user_ctx.get("role") or "free", "admin") else _owner(user_ctx)


def _version(request: Request, *, creating: bool) -> str:
    """The request's ``anthropic-version``, checked; ``anthropic-beta`` refused."""
    if request.headers.get("anthropic-beta"):
        raise HTTPException(
            400, "anthropic-beta is not supported for Message Batches on this gateway"
        )
    version = (request.headers.get("anthropic-version") or "").strip()
    if not version:
        raise HTTPException(400, "anthropic-version: header is required")
    allowed = batch_config().anthropic_versions if creating else KNOWN_ANTHROPIC_VERSIONS
    if version not in allowed:
        raise HTTPException(
            400,
            f"anthropic-version: {version!r} is not supported for Message Batches here; "
            f"use one of {', '.join(allowed)}",
        )
    return version


def _results_base(request: Request) -> str:
    """The batches collection URL under the prefix this request used."""
    prefix = "/anthropic" if request.url.path.startswith("/anthropic/") else ""
    return f"{str(request.base_url).rstrip('/')}{prefix}/v1/messages/batches"


def _not_found(batch_id: str) -> HTTPException:
    return HTTPException(404, f"Message batch '{batch_id}' not found")


def _parse(raw: bytes | bytearray, max_requests: int) -> list[dict[str, Any]]:
    """The ``requests`` of a create body, envelope-checked (400 on the first problem)."""
    try:
        decoded = json.loads(raw, parse_constant=admission.reject_json_constant)
    except (ValueError, RecursionError):
        raise HTTPException(400, "Request body is not valid JSON") from None
    if admission.has_invalid_unicode(decoded):
        raise HTTPException(400, "Request body holds a string that is not valid Unicode")
    if not isinstance(decoded, dict) or not isinstance(decoded.get("requests"), list):
        raise HTTPException(400, "requests: Field required")
    unknown = sorted(set(decoded) - {"requests"})
    if unknown:
        raise HTTPException(400, f"{', '.join(unknown)}: Extra inputs are not permitted")
    requests = decoded["requests"]
    limit = min(max_requests, _PROTOCOL_MAX_REQUESTS)
    if not requests:
        raise HTTPException(400, "requests: List should have at least 1 item")
    if len(requests) > limit:
        raise HTTPException(
            400,
            f"requests: a batch may hold at most {limit} requests; this one has {len(requests)}",
        )
    seen: set[str] = set()
    for index, entry in enumerate(requests):
        if not isinstance(entry, dict):
            raise HTTPException(400, f"requests.{index}: must be an object")
        custom_id = entry.get("custom_id")
        if not isinstance(custom_id, str) or not _CUSTOM_ID.fullmatch(custom_id):
            raise HTTPException(
                400,
                f"requests.{index}.custom_id: must match ^[a-zA-Z0-9_-]{{1,64}}$",
            )
        if custom_id in seen:
            raise HTTPException(
                400, f"requests.{index}.custom_id: {custom_id!r} is not unique within the batch"
            )
        seen.add(custom_id)
        if not isinstance(entry.get("params"), dict):
            raise HTTPException(400, f"requests.{index}.params: must be an object")
        extra = sorted(set(entry) - {"custom_id", "params"})
        if extra:
            raise HTTPException(
                400, f"requests.{index}: {', '.join(extra)}: Extra inputs are not permitted"
            )
    return requests


@router.post("/v1/messages/batches", response_model=None)
@router.post("/anthropic/v1/messages/batches", response_model=None)
async def create_message_batch(
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
    services=Depends(get_services),
    router_exec=Depends(get_router),
    visibility_resolver=Depends(get_model_visibility_resolver),
) -> JSONResponse:
    """Create a Message Batch."""
    config = batch_config()
    # Any known version parses and replays; whether it may create a new batch
    # is decided only once the request is not a replay.
    version = _version(request, creating=False)
    owner = _owner(user_ctx)
    key = admission.idempotency_key(request)
    raw = await admission.read_body(request, config.max_body_bytes)
    requests = _parse(raw, config.max_requests)
    digest = admission.request_digest(SURFACE_ANTHROPIC, requests=requests, version=version)
    replay = await admission.find_replay(
        store, owner=owner, surface=SURFACE_ANTHROPIC, key=key, digest=digest
    )
    if replay is not None:
        return JSONResponse(anthropic_batch(replay, results_base=_results_base(request)))
    if not may_use_batch(user_ctx.get("role"), config):
        raise HTTPException(
            403, f"Creating batches requires the {config.min_role!r} role or higher"
        )
    enforce_daily_quota(request, user_ctx)
    _version(request, creating=True)
    if key is None:
        # With an Idempotency-Key the insert transaction decides the cap, so a
        # concurrent identical create is answered with its batch (see /v1/batches).
        await admission.precheck_active(store, owner, config)

    ctx = AdmissionContext(
        user_ctx=user_ctx,
        routes=router_exec.routes,
        services=services,
        visibility_resolver=visibility_resolver,
        embedding_adapters=None,
        profile=PROFILES[version],
    )
    items: list[dict[str, Any]] = []
    models: set[str] = set()
    for entry in requests:
        params = entry["params"]
        canonical, problem = await check_item(TARGET_MESSAGES, params, ctx)
        if problem is not None or canonical is None:
            model = params.get("model")
            items.append(
                {
                    "custom_id": entry["custom_id"],
                    "model_id": model if isinstance(model, str) else "",
                    "request": params,
                    "error": problem.as_error() if problem else {"code": "invalid_request"},
                }
            )
            continue
        models.add(canonical)
        items.append({"custom_id": entry["custom_id"], "model_id": canonical, "request": params})
    row = await admission.create(
        store,
        batch_id=f"msgbatch_{uuid.uuid4().hex}",
        user_id=owner,
        key_hash=user_ctx.get("auth_key_hash"),
        endpoint="/v1/messages",
        model_ids=sorted(models),
        items=items,
        metadata=None,
        completion_window_seconds=PROTOCOL_WINDOW_SECONDS,
        api_surface=SURFACE_ANTHROPIC,
        target=TARGET_MESSAGES,
        protocol_profile=version,
        retention_seconds=config.retention_seconds,
        idempotency={"key": key, "digest": digest} if key else None,
        max_active=config.max_active_per_user,
        max_queued=config.max_queued_per_user,
    )
    if not row.get("replayed"):
        logger.info(
            "Message batch %s created: %d requests (%d refused at admission), version %s",
            row["id"],
            len(items),
            sum(1 for item in items if item.get("error")),
            version,
            extra={"user_id": owner},
        )
    admission.wake_worker(services)
    return JSONResponse(anthropic_batch(row, results_base=_results_base(request)))


@router.get("/v1/messages/batches", response_model=None)
@router.get("/anthropic/v1/messages/batches", response_model=None)
async def list_message_batches(
    request: Request,
    limit: int = Query(20, ge=1, le=1000),
    before_id: str | None = Query(None),
    after_id: str | None = Query(None),
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> JSONResponse:
    """List the caller's Message Batches, newest first."""
    _version(request, creating=False)
    if before_id and after_id:
        raise HTTPException(400, "before_id and after_id cannot both be set")
    rows = await store.list_batches(
        user_id=_owner(user_ctx),
        limit=limit + 1,
        after=after_id,
        before=before_id,
        surfaces=_SURFACES,
    )
    if before_id:
        has_more = len(rows) > limit
        rows = rows[-limit:] if has_more else rows
    else:
        has_more = len(rows) > limit
        rows = rows[:limit]
    base = _results_base(request)
    data = [anthropic_batch(row, results_base=base) for row in rows]
    return JSONResponse(
        {
            "data": data,
            "has_more": has_more,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }
    )


@router.get("/v1/messages/batches/{batch_id}", response_model=None)
@router.get("/anthropic/v1/messages/batches/{batch_id}", response_model=None)
async def retrieve_message_batch(
    batch_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> JSONResponse:
    """Return a Message Batch."""
    _version(request, creating=False)
    row = await store.get_batch(batch_id, user_id=_lookup_scope(user_ctx), surfaces=_SURFACES)
    if row is None:
        raise _not_found(batch_id)
    return JSONResponse(anthropic_batch(row, results_base=_results_base(request)))


@router.post("/v1/messages/batches/{batch_id}/cancel", response_model=None)
@router.post("/anthropic/v1/messages/batches/{batch_id}/cancel", response_model=None)
async def cancel_message_batch(
    batch_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> JSONResponse:
    """Cancel a Message Batch: it is ``canceling`` until its running requests finish."""
    _version(request, creating=False)
    row = await store.cancel_batch(batch_id, user_id=_lookup_scope(user_ctx), surfaces=_SURFACES)
    if row is None:
        raise _not_found(batch_id)
    return JSONResponse(anthropic_batch(row, results_base=_results_base(request)))


@router.delete("/v1/messages/batches/{batch_id}", response_model=None)
@router.delete("/anthropic/v1/messages/batches/{batch_id}", response_model=None)
async def delete_message_batch(
    batch_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> JSONResponse:
    """Delete an ended Message Batch with its results."""
    _version(request, creating=False)
    try:
        deleted = await store.delete_batch(
            batch_id, user_id=_lookup_scope(user_ctx), surfaces=_SURFACES, require_ended=True
        )
    except BatchNotEnded:
        raise HTTPException(
            400,
            f"Message batch '{batch_id}' has not ended; cancel it and wait for it to end "
            "before deleting it",
        ) from None
    except BatchDeleteInProgress:
        raise HTTPException(
            400, f"Message batch '{batch_id}' still has a running request"
        ) from None
    if not deleted:
        raise _not_found(batch_id)
    return JSONResponse({"id": batch_id, "type": "message_batch_deleted"})


@router.get("/v1/messages/batches/{batch_id}/results", response_model=None)
@router.get("/anthropic/v1/messages/batches/{batch_id}/results", response_model=None)
async def message_batch_results(
    batch_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_batch_management_identity),
    store=Depends(get_batch_store),
) -> StreamingResponse:
    """Stream an ended Message Batch's results as JSONL, one line per request."""
    _version(request, creating=False)
    row = await store.get_batch(batch_id, user_id=_lookup_scope(user_ctx), surfaces=_SURFACES)
    if row is None:
        raise _not_found(batch_id)
    if row.get("phase") != PHASE_SEALED:
        raise HTTPException(
            400, f"Message batch '{batch_id}' has not ended; its results are not available yet"
        )
    return StreamingResponse(
        stream_lines(store=store, batch_id=batch_id, surface=SURFACE_ANTHROPIC, kind="all"),
        media_type="application/x-jsonl",
    )
