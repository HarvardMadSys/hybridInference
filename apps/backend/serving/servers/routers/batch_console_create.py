"""Creating a batch in the console: upload, preflight and submit, for the signed-in user.

The session counterpart of ``/v1/files`` + ``/v1/batches`` for the console's
create page. It goes through the same upload, admission, store and worker as
the API -- there is no second pipeline -- and adds what a person needs in
front of it: a strict, read-only preflight that identifies and checks the whole
file without running anything (:mod:`serving.batch.console_prepare`), a few
explicit overrides, and a submission bound to what was confirmed.

- Every route acts for the signed-in user only (``Authorization: Bearer``
  session token; no cookie), administrators included: another user's file,
  batch or submission is not found. ``BATCH_ENABLED`` off answers 404.
- Uploading, preflight and submitting need the role ``BATCH_MIN_ROLE``.
  Uploading and preflight spend nothing: no API key, no quota.
- A submission runs under the account's **existing active API key**, looked
  up here, never shown and never created: the worker re-authenticates it
  before every request, as for a batch the API created. The page confirms the
  key's prefix and an opaque revision; a key revoked, regenerated or replaced
  since answers 409 ``credential_changed``, and no key 409
  ``no_worker_credential``. Signing out does not stop a submitted batch;
  revoking or regenerating the key does.
- The model is bound too: the canonical id the preview showed must still be
  what the choice resolves to (409 ``model_resolution_changed``), and the
  stored requests name it.
- A submission carries an ``Idempotency-Key``. It is decided under the owner's
  admission lock before the caps, so a retry of a submission that committed
  returns its batch even when the active-batch cap is full now. A console
  batch keeps its key and request digest, so a retry still finds it after the
  idempotency record expired (a day after the batch ended) for as long as the
  batch is stored; ``GET /user/batch-submissions/{key}`` reads that without
  creating anything.

Design: ``docs/agents/specs/2026-10-28-batch-ui-create-design.zh.md``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from serving.batch import admission
from serving.batch.config import PROTOCOL_WINDOW_SECONDS, batch_config, may_use_batch
from serving.batch.console_prepare import (
    CONSOLE_TARGETS,
    ConsoleScan,
    DerivedWriter,
    Overrides,
    ScanBusy,
    ScanTimeout,
    SourceUnavailable,
    scan_slot,
)
from serving.batch.delivery import stream_upload
from serving.batch.targets import AdmissionContext
from serving.model_access import get_disabled_models_from_preferences
from serving.servers.auth import (
    StoredKeyQuotaExceeded,
    StoredKeyRejected,
    authenticate_stored_key,
    is_user_auth_enabled,
)
from serving.servers.deps import (
    get_batch_file_store,
    get_batch_store,
    get_current_user,
    get_embedding_adapters,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
    get_services,
)
from serving.servers.routers.batch_console import COPYABLE_TARGETS, batch_detail
from serving.servers.routers.files import content_disposition, file_object, store_upload
from serving.servers.routers.models import _build_model_list_async
from serving.storage.batch_file_store import BatchFileQuotaExceeded
from serving.storage.batch_store import (
    IDEMPOTENCY_RETENTION_SECONDS,
    SURFACE_OPENAI_FILE,
    BatchInputUnavailable,
    BatchLimitExceeded,
    BatchOwnerUnavailable,
    IdempotencyConflict,
    IdempotencyTombstoned,
)
from serving.utils.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)

_UNAVAILABLE = "Batch inference is not available on this gateway"
_MAX_NAME = 128
_MAX_DESCRIPTION = 1000


# -- shared --------------------------------------------------------------------------


def _surface(store: Any, file_store: Any) -> tuple[Any, Any, Any]:
    config = batch_config()
    if store is None or file_store is None or not config.enabled:
        raise HTTPException(404, _UNAVAILABLE)
    return store, file_store, config


def _error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    """An error the create page acts on: ``{"error": {"type", "code", "message", ...}}``.

    The ``error`` envelope of every gateway error, with ``code`` the reason
    the page branches on (``credential_changed``, ``source_unavailable``...).
    """
    return HTTPException(
        status,
        {"error": {"type": "batch_console_error", "code": code, "message": message, **extra}},
    )


def _may_submit(current_user: dict[str, Any], config: Any) -> None:
    if not may_use_batch(current_user.get("role"), config):
        raise _error(
            403,
            "role_required",
            f"Creating batches requires the {config.min_role!r} role or higher",
        )


async def session_model_context(current_user: dict[str, Any], op_store: Any) -> dict[str, Any]:
    """The signed-in user as model access sees them, without an API key.

    The session context with the account's own model preferences
    (``disabled_models``), read as ``/user/models`` and an API key read them,
    so a preflight refuses exactly the models a submission would. It has no
    quota and no key: a preflight spends nothing.
    """
    disabled: list[str] = []
    if op_store:
        disabled = get_disabled_models_from_preferences(
            await op_store.get_user_preferences(current_user["user_id"])
        )
    return {**current_user, "disabled_models": disabled}


def _admission_context(
    user_ctx: dict[str, Any], *, services: Any, router_exec: Any, visibility_resolver: Any
) -> AdmissionContext:
    return AdmissionContext(
        user_ctx=user_ctx,
        routes=router_exec.routes,
        services=services,
        visibility_resolver=visibility_resolver,
        embedding_adapters=getattr(services, "embedding_adapters", None),
        embedding_routes=getattr(router_exec, "embedding_routes", None),
    )


async def _catalog(
    user_ctx: dict[str, Any], *, router_exec: Any, embedding_adapters: Any, visibility_resolver: Any
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """The models the user may pick, by type, and the limits of each (by canonical id)."""
    catalog = await _build_model_list_async(
        router_exec=router_exec,
        embedding_adapters=embedding_adapters or {},
        model_visibility_resolver=visibility_resolver,
        user_ctx=user_ctx,
    )
    models: dict[str, list[dict[str, Any]]] = {"chat": [], "embeddings": []}
    info: dict[str, dict[str, Any]] = {}
    for item in catalog.data:
        kind = "embeddings" if "embeddings" in (item.supported_features or []) else "chat"
        entry = {
            "id": item.id,
            "name": item.name,
            "max_output_length": item.max_output_length,
            "supported_sampling_parameters": list(item.supported_sampling_parameters or []),
        }
        models[kind].append(entry)
        info[item.id] = entry
    return models, info


def credential_revision(key_id: Any, key_hash: str) -> str:
    """An opaque, non-secret token naming one state of one API key.

    It changes when the key is replaced, revoked and re-created, or
    regenerated in place (which keeps the row and changes its hash), so a
    confirmation cannot outlive the key it saw. One-way and domain-separated:
    it reveals neither the key nor its stored hash.
    """
    material = f"hybridinference:batch-console-credential:v1:{key_id}:{key_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _credential_view(credential: dict[str, Any] | None) -> dict[str, Any] | None:
    if credential is None:
        return None
    return {
        "key_id": str(credential["key_id"]),
        "key_prefix": credential["key_prefix"],
        "revision": credential_revision(credential["key_id"], credential["key_hash"]),
    }


def _epoch(value: Any) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None


async def _own_source(file_store: Any, file_id: str, owner: str) -> dict[str, Any]:
    """The user's own ready source upload, or 409 ``source_unavailable``."""
    row = await file_store.get_file(file_id, user_id=owner)
    if (
        row is None
        or row.get("kind") != "upload"
        or row.get("purpose") != "batch"
        or row.get("status") != "ready"
        or row.get("derived_from_file_id")
    ):
        raise _error(
            409,
            "source_unavailable",
            "The source file is no longer available (deleted or expired); upload it again",
        )
    return row


def _storage_full(exc: BatchFileQuotaExceeded) -> HTTPException:
    return _error(
        413,
        "storage_full",
        f"Storing these requests would exceed {exc.limit // (1024 * 1024)} MiB of batch "
        "files; delete files you no longer need",
    )


def _scan_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ScanBusy):
        return _error(429, "scan_busy", "Another file is being checked; try again in a few seconds")
    if isinstance(exc, ScanTimeout):
        return _error(503, "scan_timeout", "Checking the file took too long; try again later")
    return _error(
        409,
        "source_unavailable",
        "The source file is no longer available (deleted or expired); upload it again",
    )


# -- context -----------------------------------------------------------------------------


@router.get("/user/batch-create/context")
async def create_context(
    response: Response,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
    op_store=Depends(get_operational_store),
    router_exec=Depends(get_router),
    embedding_adapters=Depends(get_embedding_adapters),
    visibility_resolver=Depends(get_model_visibility_resolver),
) -> dict[str, Any]:
    """What the create page shows: limits, usage, the run credential and the models.

    The credential is the active key's prefix and revision -- never the key
    or its hash. ``quota`` is read without being enforced; null without a key.
    """
    store, file_store, config = _surface(store, file_store)
    response.headers["Cache-Control"] = "no-store"
    owner = current_user["user_id"]
    credential = await store.active_batch_credential(owner)
    quota = None
    if credential is not None and is_user_auth_enabled():
        try:
            ctx = await authenticate_stored_key(credential["key_hash"], op_store, check_quota=False)
            quota = {
                "daily_usd": ctx.get("quota_daily_cost_usd"),
                "spent_today_usd": ctx.get("spent_today_usd"),
            }
        except Exception:
            logger.info("Batch create context: the key's quota could not be read", exc_info=True)
    session_ctx = await session_model_context(current_user, op_store)
    models, _info = await _catalog(
        session_ctx,
        router_exec=router_exec,
        embedding_adapters=embedding_adapters,
        visibility_resolver=visibility_resolver,
    )
    usage = await file_store.usage(owner)
    return {
        "user_id": owner,
        "role_may_submit": may_use_batch(current_user.get("role"), config),
        "min_role": config.min_role,
        "submission_enabled": is_user_auth_enabled(),
        "limits": {
            "max_file_bytes": config.max_body_bytes,
            "max_line_bytes": config.max_line_bytes,
            "max_requests": config.max_requests,
            "uploads_per_hour": config.file_uploads_per_hour,
            "files_max_total_bytes": config.files_max_total_bytes,
            "max_active_batches": config.max_active_per_user,
            "file_retention_days": config.file_retention_days,
            "result_retention_days": config.retention_days,
            "completion_window_hours": PROTOCOL_WINDOW_SECONDS // 3600,
            "submission_replay_seconds": IDEMPOTENCY_RETENTION_SECONDS,
        },
        "usage": {
            **usage,
            "active_batches": await store.count_active_batches(owner),
        },
        "credential": _credential_view(credential),
        "quota": quota,
        "models": models,
    }


# -- source files -----------------------------------------------------------------------


def _source_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **file_object(row),
        "sha256": row.get("sha256"),
        "line_count": row.get("line_count"),
    }


@router.post("/user/batch-files")
async def upload_source(
    request: Request,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
) -> JSONResponse:
    """Upload a JSONL source file, through the same path and limits as ``/v1/files``."""
    _store, file_store, config = _surface(store, file_store)
    _may_submit(current_user, config)
    row = await store_upload(
        request,
        file_store=file_store,
        owner=current_user["user_id"],
        check_owner=True,
        config=config,
    )
    return JSONResponse(_source_view(row), headers={"Cache-Control": "no-store"})


@router.get("/user/batch-files")
async def list_sources(
    response: Response,
    limit: int = Query(100, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
) -> dict[str, Any]:
    """The signed-in user's own uploaded source files, newest first, and their storage use."""
    _store, file_store, config = _surface(store, file_store)
    response.headers["Cache-Control"] = "no-store"
    owner = current_user["user_id"]
    rows = await file_store.list_files(
        user_id=owner, limit=limit + 1, purpose="batch", sources_only=True
    )
    return {
        "object": "list",
        "data": [_source_view(row) for row in rows[:limit]],
        "has_more": len(rows) > limit,
        "usage": await file_store.usage(owner),
        "files_max_total_bytes": config.files_max_total_bytes,
    }


async def _owned_upload(file_store: Any, file_id: str, owner: str) -> dict[str, Any]:
    row = await file_store.get_file(file_id, user_id=owner)
    if row is None or row.get("kind") != "upload" or row.get("status") != "ready":
        raise HTTPException(404, f"File '{file_id}' not found")
    return row


@router.delete("/user/batch-files/{file_id}")
async def delete_source(
    file_id: str,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
) -> dict[str, Any]:
    """Delete one of the user's source files (batches already submitted keep running)."""
    _store, file_store, _config = _surface(store, file_store)
    owner = current_user["user_id"]
    row = await _owned_upload(file_store, file_id, owner)
    if row.get("derived_from_file_id") or not await file_store.delete_file(file_id, user_id=owner):
        raise HTTPException(404, f"File '{file_id}' not found")
    return {"id": file_id, "deleted": True}


@router.get("/user/batch-files/{file_id}/content", response_model=None)
async def download_source(
    file_id: str,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
) -> StreamingResponse:
    """Download one of the user's uploads (a source, or a batch's stored-request copy) as stored."""
    store, file_store, _config = _surface(store, file_store)
    row = await _owned_upload(file_store, file_id, current_user["user_id"])
    return StreamingResponse(
        stream_upload(store=store, file_store=file_store, file_id=file_id),
        media_type="application/jsonl",
        headers={
            "content-length": str(int(row.get("bytes") or 0)),
            "content-disposition": content_disposition(row.get("filename") or file_id),
            "cache-control": "no-store",
        },
    )


# -- preflight ---------------------------------------------------------------------------


class OverridesBody(BaseModel):
    """The values step 2 sets on every row."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1, max_length=256)
    temperature: float | None = None
    max_tokens: int | None = None

    def as_overrides(self) -> Overrides:
        """As the scanner takes them."""
        return Overrides(model=self.model, temperature=self.temperature, max_tokens=self.max_tokens)


class PreflightBody(BaseModel):
    """A preflight: identify the file, or validate it with the chosen settings."""

    model_config = ConfigDict(extra="forbid")

    source_file_id: str = Field(min_length=1, max_length=64)
    phase: str = Field(pattern="^(identify|validate)$")
    #: The type, only when the file does not say it (``needs_type_choice``).
    endpoint: str | None = Field(default=None, max_length=64)
    overrides: OverridesBody = Field(default_factory=OverridesBody)


@router.post("/user/batch-preflight")
async def preflight(
    body: PreflightBody,
    response: Response,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
    op_store=Depends(get_operational_store),
    services=Depends(get_services),
    router_exec=Depends(get_router),
    embedding_adapters=Depends(get_embedding_adapters),
    visibility_resolver=Depends(get_model_visibility_resolver),
) -> dict[str, Any]:
    """Identify and check a source file, read only: nothing is stored, run or billed.

    Model access is the account's own (role, visibility, disabled models) --
    no API key or quota is needed to check a file.
    """
    store, file_store, config = _surface(store, file_store)
    response.headers["Cache-Control"] = "no-store"
    _may_submit(current_user, config)
    owner = current_user["user_id"]
    source = await _own_source(file_store, body.source_file_id, owner)
    user_ctx = await session_model_context(current_user, op_store)
    _models, info = await _catalog(
        user_ctx,
        router_exec=router_exec,
        embedding_adapters=embedding_adapters,
        visibility_resolver=visibility_resolver,
    )
    scan = ConsoleScan(
        store=store,
        file_store=file_store,
        ctx=_admission_context(
            user_ctx,
            services=services,
            router_exec=router_exec,
            visibility_resolver=visibility_resolver,
        ),
        config=config,
        source=source,
        overrides=body.overrides.as_overrides(),
        endpoint_choice=body.endpoint,
        model_info=info,
    )
    try:
        with scan_slot(owner):
            await scan.run(validate=body.phase == "validate")
    except (ScanBusy, ScanTimeout, SourceUnavailable) as exc:
        raise _scan_error(exc) from None
    report = scan.report()
    report["source"]["expires_at"] = _epoch(source.get("expires_at"))
    return report


# -- submitting --------------------------------------------------------------------------


class ConfirmedModel(BaseModel):
    """The model the confirmation showed: the choice and the canonical id it resolved to."""

    model_config = ConfigDict(extra="forbid")

    requested: str = Field(min_length=1, max_length=256)
    canonical: str = Field(min_length=1, max_length=256)


class ConfirmedCredential(BaseModel):
    """The run credential the confirmation showed (never the key itself)."""

    model_config = ConfigDict(extra="forbid")

    key_id: str = Field(min_length=1, max_length=64)
    revision: str = Field(min_length=1, max_length=64)


class CreateBody(BaseModel):
    """A console submission: the confirmed source, type, overrides, model and credential."""

    model_config = ConfigDict(extra="forbid")

    source_file_id: str = Field(min_length=1, max_length=64)
    source_sha256: str = Field(min_length=64, max_length=64)
    endpoint: str = Field(min_length=1, max_length=64)
    overrides: OverridesBody = Field(default_factory=OverridesBody)
    confirmed_model: ConfirmedModel
    credential: ConfirmedCredential
    name: str = Field(min_length=1, max_length=_MAX_NAME)
    description: str = Field(default="", max_length=_MAX_DESCRIPTION)
    copied_from: str | None = Field(default=None, max_length=128)


def _check_text(body: CreateBody) -> None:
    if not body.name.strip() or any(not char.isprintable() for char in body.name):
        raise _error(422, "invalid_name", "The name must be 1 to 128 printable characters")
    if any(not char.isprintable() and char not in "\n\t" for char in body.description):
        raise _error(422, "invalid_description", "The description holds a control character")


def _digest(body: CreateBody) -> str:
    return admission.request_digest(
        "console",
        **json.loads(body.model_dump_json(exclude_none=False)),
    )


async def _replay(store: Any, owner: str, key: str, digest: str) -> dict[str, Any] | None:
    """The batch this key already created, from its live record or, once that expired, its row.

    Raises:
        HTTPException: 409 when the key was used for another submission, 410
            when its batch was deleted.
    """
    row = await admission.find_replay(
        store, owner=owner, surface=SURFACE_OPENAI_FILE, key=key, digest=digest
    )
    if row is not None:
        return row
    found = (await store.find_console_submission(user_id=owner, key=key))["batch"]
    if found is None:
        return None
    if (found.get("console_spec") or {}).get("request_digest") != digest:
        raise admission.conflict_error()
    return found


@router.get("/user/batch-submissions/{key}")
async def find_submission(
    key: str,
    response: Response,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
) -> dict[str, Any]:
    """What a console submission key created, read only (for a page recovering a submission).

    ``state``: ``created`` (with the batch), ``deleted`` (it created a batch
    that has since been deleted) or ``unknown`` -- nothing under this key is
    stored any more, or it never committed; the two can no longer be told
    apart, so this never answers "not created". Only the signed-in user's.
    """
    store, file_store, _config = _surface(store, file_store)
    response.headers["Cache-Control"] = "no-store"
    owner = current_user["user_id"]
    found = await store.find_console_submission(user_id=owner, key=key[:255])
    if found["batch"] is not None:
        return {
            "state": "created",
            "batch": await batch_detail(found["batch"], file_store=file_store, owner=owner),
        }
    if found["record"] == "deleted":
        return {"state": "deleted", "batch": None}
    return {"state": "unknown", "batch": None}


@router.post("/user/batches")
async def create_console_batch(
    body: CreateBody,
    request: Request,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
    file_store=Depends(get_batch_file_store),
    op_store=Depends(get_operational_store),
    services=Depends(get_services),
    router_exec=Depends(get_router),
    embedding_adapters=Depends(get_embedding_adapters),
    visibility_resolver=Depends(get_model_visibility_resolver),
) -> JSONResponse:
    """Create a Chat or Embeddings batch from a source file, as the preview showed it.

    In order: the idempotent replay (before anything that could refuse), the
    role, the run credential (the confirmed one, still active: its owner's
    current key, with the daily quota), the source (the user's, still
    available, the bytes previewed), a full scan under the key's model access
    with the confirmed model checked and the stored requests written when they
    differ from the source, then one transaction that checks the replay again,
    the caps and the source, adopts the stored copy and stores the batch.
    """
    store, file_store, config = _surface(store, file_store)
    owner = current_user["user_id"]
    key = admission.idempotency_key(request)
    if key is None:
        raise _error(400, "idempotency_key_required", "An Idempotency-Key header is required")
    digest = _digest(body)
    replay = await _replay(store, owner, key, digest)
    if replay is not None:
        return _created(await batch_detail(replay, file_store=file_store, owner=owner))

    _may_submit(current_user, config)
    _check_text(body)
    target = CONSOLE_TARGETS.get(body.endpoint)
    if target is None:
        raise _error(
            422,
            "unsupported_endpoint",
            "The console creates /v1/chat/completions and /v1/embeddings batches only",
        )
    if not is_user_auth_enabled():
        raise _error(
            403,
            "console_submit_unavailable",
            "Submitting from the console needs user authentication; use /v1/batches",
        )
    credential = await store.active_batch_credential(owner)
    if credential is None:
        raise _error(
            409,
            "no_worker_credential",
            "A batch runs under your active API key, and you have none; create one first",
        )
    if (
        str(credential["key_id"]) != body.credential.key_id
        or credential_revision(credential["key_id"], credential["key_hash"])
        != body.credential.revision
    ):
        raise _error(
            409,
            "credential_changed",
            "Your API key changed since you confirmed; check the submission again",
        )
    try:
        user_ctx = await authenticate_stored_key(credential["key_hash"], op_store)
    except StoredKeyQuotaExceeded as exc:
        raise _error(
            429,
            "quota_exceeded",
            "Your daily quota is spent; submit after it renews",
            reset_at=_epoch(exc.reset_at),
        ) from None
    except StoredKeyRejected:
        raise _error(
            409,
            "credential_changed",
            "Your API key can no longer run batches (revoked, expired, or the account is not "
            "verified); check the submission again",
        ) from None
    if user_ctx.get("user_id") not in (None, owner):
        raise _error(409, "credential_changed", "Your API key changed since you confirmed")
    if body.copied_from is not None:
        original = await store.get_batch(body.copied_from, user_id=owner)
        if original is None:
            raise HTTPException(404, f"Batch '{body.copied_from}' not found")
        # The same rule the details page shows (copy.state): only a file batch
        # of a type the console creates is copied -- with its source kept or
        # uploaded again; direct requests, Messages and Responses are not.
        if original.get("api_surface") != SURFACE_OPENAI_FILE or (
            original.get("target") not in COPYABLE_TARGETS
        ):
            raise _error(
                422,
                "copy_unsupported",
                "That batch cannot be copied in the console (direct requests, or a type the "
                "console does not create)",
            )
    source = await _own_source(file_store, body.source_file_id, owner)
    if source.get("sha256") != body.source_sha256:
        raise _error(409, "source_unavailable", "The source file is not the one you checked")

    _models, info = await _catalog(
        user_ctx,
        router_exec=router_exec,
        embedding_adapters=embedding_adapters,
        visibility_resolver=visibility_resolver,
    )
    overrides = body.overrides.as_overrides()
    scan = ConsoleScan(
        store=store,
        file_store=file_store,
        ctx=_admission_context(
            user_ctx,
            services=services,
            router_exec=router_exec,
            visibility_resolver=visibility_resolver,
        ),
        config=config,
        source=source,
        overrides=overrides,
        endpoint_choice=body.endpoint,
        model_info=info,
    )
    writer = DerivedWriter(
        file_store,
        user_id=owner,
        source_file_id=source["id"],
        filename=f"{source.get('filename') or 'requests'}.stored.jsonl",
        cap=config.files_max_total_bytes,
    )
    try:
        try:
            with scan_slot(owner):
                await scan.run(validate=True, writer=writer)
        except (ScanBusy, ScanTimeout, SourceUnavailable) as exc:
            raise _scan_error(exc) from None
        except BatchFileQuotaExceeded as exc:
            raise _storage_full(exc) from None
        if not scan.valid:
            raise _error(
                422,
                "invalid_file",
                "The file has problems; nothing was submitted",
                report=scan.report(),
            )
        if scan.target != target:
            raise _error(409, "mismatched_endpoint", "The file is not of the confirmed type")
        if scan.canonical != body.confirmed_model.canonical or (
            overrides.model is not None and overrides.model != body.confirmed_model.requested
        ):
            raise _error(
                409,
                "model_resolution_changed",
                "The model now resolves to another one; check the submission again",
                canonical=scan.canonical,
            )
        try:
            # A copy under one chunk is still buffered here: its first (and
            # only) write to storage, and the cap check, happen now.
            derived = await writer.finish() if scan.stores_copy and writer.file_id else None
        except BatchFileQuotaExceeded as exc:
            raise _storage_full(exc) from None
        if scan.stores_copy and derived is None:
            raise _error(409, "source_unavailable", "The stored requests could not be prepared")
        spec = {
            "source_file_id": source["id"],
            "source_sha256": source.get("sha256"),
            "source_filename": source.get("filename"),
            "endpoint": body.endpoint,
            "type_source": scan.type_source,
            "overrides": overrides.as_dict(),
            "confirmed_model": body.confirmed_model.model_dump(),
            "credential_key_id": body.credential.key_id,
            "credential_key_prefix": credential["key_prefix"],
            "copied_from": body.copied_from,
            "stores_copy": scan.stores_copy,
            "request_digest": digest,
        }
        try:
            row = await store.create_console_batch(
                batch_id=f"batch_{uuid.uuid4().hex}",
                user_id=owner,
                key_hash=credential["key_hash"],
                endpoint=body.endpoint,
                target=target,
                source_file_id=source["id"],
                source_sha256=body.source_sha256,
                derived=derived,
                file_retention_seconds=config.file_retention_days * 86400,
                completion_window_seconds=PROTOCOL_WINDOW_SECONDS,
                retention_seconds=config.retention_seconds,
                idempotency={"key": key, "digest": digest},
                max_active=config.max_active_per_user,
                max_queued=config.max_queued_per_user,
                console={
                    "display_name": body.name.strip(),
                    "description": body.description.strip() or None,
                    "console_spec": spec,
                },
            )
        except BatchOwnerUnavailable:
            raise HTTPException(401, "Your account is no longer active") from None
        except BatchInputUnavailable:
            raise _error(
                409,
                "source_unavailable",
                "The source file was deleted or expired while it was submitted; upload it again",
            ) from None
        except BatchLimitExceeded as exc:
            raise admission.limit_error(exc) from None
        except IdempotencyConflict:
            raise admission.conflict_error() from None
        except IdempotencyTombstoned:
            raise admission.gone_error() from None
    except BaseException:
        await writer.discard()
        raise
    if row.get("replayed"):
        await writer.discard()
    else:
        logger.info(
            "Console batch %s created (%s): %d requests%s",
            row["id"],
            body.endpoint,
            scan.request_count,
            ", stored copy" if derived else "",
            extra={"user_id": owner},
        )
    admission.wake_worker(services)
    return _created(await batch_detail(row, file_store=file_store, owner=owner))


def _created(detail: dict[str, Any]) -> JSONResponse:
    return JSONResponse(detail, headers={"Cache-Control": "no-store"})
