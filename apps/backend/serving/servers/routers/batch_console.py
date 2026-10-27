"""Batch management for people: the console's batch page, and webhook settings.

The protocol routes (``/v1/batches``, ``/v1/messages/batches``, ``/v1/files``)
are for programs holding an API key. This router serves the same batches to
the signed-in user of the console (``/user/batches``, session token), across
every API surface at once, with the counts, tokens and known cost of each, and
lets them cancel, delete and download results. Nothing here creates a batch.
These owner routes do not check ``BATCH_MIN_ROLE``: a user whose role was
lowered keeps managing what they already have.

``/user/batch-capabilities`` tells the console what to offer: whether Batch is
on, whether the user's role may submit, and whether they have batches or a
webhook to manage -- so a user below the role who has history keeps the
dashboard entry, and one without it sees none.

Webhook settings exist twice over the same store: ``/user/batch-webhook`` for
the console and ``/v1/batch_webhook`` for an API key. A user has at most one
webhook; its signing secret is generated here and shown once, when the webhook
is created or the secret rotated, and is stored encrypted. Writing one (create,
update, rotate, re-enable) needs the role that may create batches,
``BATCH_MIN_ROLE``, checked on the caller's current role before the URL is
resolved or the store touched. Reading, listing events and deleting stay open
to the owner, so a user whose role was lowered can still see and remove theirs.
Every route writes only the caller's own webhook.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from serving.batch.config import batch_config, may_use_batch
from serving.batch.delivery import stream_lines
from serving.batch.renderers import (
    anthropic_processing_status,
    anthropic_request_counts,
    openai_status,
)
from serving.batch.webhooks import (
    new_secret,
    resolution_problem as check_resolves_public,
    url_problem,
)
from serving.servers.auth import encrypt_api_key, verify_batch_management_identity
from serving.servers.deps import (
    get_batch_store,
    get_batch_webhook_store,
    get_current_user,
)
from serving.servers.routers.batches import results_final
from serving.servers.routers.files import content_disposition
from serving.storage.batch_store import (
    SURFACE_ANTHROPIC,
    SURFACE_OPENAI_FILE,
    BatchDeleteInProgress,
)
from serving.utils.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)

_UNAVAILABLE = "Batch inference is not available on this gateway"


def _require_store(store: Any) -> Any:
    if store is None or not batch_config().enabled:
        raise HTTPException(404, _UNAVAILABLE)
    return store


def _epoch(value: Any) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None


def batch_view(row: dict[str, Any]) -> dict[str, Any]:
    """One batch as the console shows it, whatever its API surface."""
    surface = row.get("api_surface") or "openai_inline"
    if surface == SURFACE_ANTHROPIC:
        status = anthropic_processing_status(row)
        counts = anthropic_request_counts(row)
    else:
        status = openai_status(row)
        counts = {
            "total": int(row.get("request_count") or 0),
            "completed": int(row.get("completed_count") or 0),
            "failed": int(row.get("failed_count") or 0),
        }
    prompt = int(row.get("prompt_tokens") or 0)
    completion = int(row.get("completion_tokens") or 0)
    return {
        "id": row["id"],
        "user_id": row.get("user_id"),
        "api_surface": surface,
        "target": row.get("target"),
        "endpoint": row.get("endpoint"),
        "status": status,
        "phase": row.get("phase"),
        "models": row.get("model_ids") or [],
        "request_counts": counts,
        "cancelled": int(row.get("cancelled_count") or 0),
        "expired": int(row.get("expired_count") or 0),
        "usage": {
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": prompt + completion,
        },
        "cost_usd": float(row.get("cost_usd") or 0.0),
        "cost_unknown_attempts": int(row.get("unknown_cost_count") or 0),
        "created_at": _epoch(row.get("created_at")),
        "expires_at": _epoch(row.get("expires_at")),
        "ended_at": _epoch(row.get("sealed_at")),
        "results_expires_at": _epoch(row.get("results_expires_at")),
        "input_file_id": row.get("input_file_id"),
        "output_file_id": row.get("output_file_id"),
        "error_file_id": row.get("error_file_id"),
        "results_available": results_final(row),
        "metadata": row.get("metadata"),
        "error": row.get("error"),
        "validation_error": row.get("validation_error"),
        "protocol_profile": row.get("protocol_profile"),
    }


async def _owns_any(check: Any, user_id: str, what: str) -> bool | None:
    """One history lookup: True / False, or None when it failed."""
    try:
        return bool(await check(user_id))
    except Exception:
        logger.warning("Batch capabilities: looking up %s failed", what, exc_info=True)
        return None


async def _has_history(store: Any, webhook_store: Any, user_id: str) -> bool | None:
    """Whether the user has something on the Batches page to manage.

    Own batches not being deleted, or a saved webhook. True as soon as either
    lookup finds one; None when neither found one and a lookup failed, since
    "no history" would then be a guess.
    """
    batches = await _owns_any(store.has_batches, user_id, "batches")
    if batches:
        return True
    # A webhook store that is not set up has no webhook to manage.
    webhook = (
        False
        if webhook_store is None
        else await _owns_any(webhook_store.has_webhook, user_id, "the webhook")
    )
    if webhook:
        return True
    return None if batches is None or webhook is None else False


@router.get("/user/batch-capabilities")
async def my_batch_capabilities(
    response: Response,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
    webhook_store=Depends(get_batch_webhook_store),
) -> dict[str, Any]:
    """What the console offers the signed-in user for Batch.

    ``status`` is ``enabled``, ``disabled`` (``BATCH_ENABLED`` off) or
    ``unavailable`` (on, but the batch store is not ready). ``role_may_submit``
    is whether the user's current role reaches ``BATCH_MIN_ROLE`` -- not
    whether a submission would pass quotas or hold a usable key, which this
    route never reads. ``has_history`` is whether the user has batches or a
    webhook to manage (None when the lookup failed, and when not enabled: then
    nothing is read). ``show_entry`` is whether the dashboard links the
    Batches page: when the user may submit, or has (or may have) history.
    """
    response.headers["Cache-Control"] = "no-store"
    user_id = current_user["user_id"]
    config = batch_config()
    if not config.enabled:
        status = "disabled"
    elif store is None:
        status = "unavailable"
    else:
        status = "enabled"
    role_may_submit = status == "enabled" and may_use_batch(current_user.get("role"), config)
    has_history = await _has_history(store, webhook_store, user_id) if status == "enabled" else None
    return {
        "user_id": user_id,
        "status": status,
        "enabled": config.enabled,
        "min_role": config.min_role,
        "role_may_submit": role_may_submit,
        "has_history": has_history,
        "history_error": status == "enabled" and has_history is None,
        "show_entry": status == "enabled" and (role_may_submit or has_history is not False),
    }


@router.get("/user/batches")
async def list_my_batches(
    limit: int = Query(20, ge=1, le=100),
    after: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
) -> dict[str, Any]:
    """The signed-in user's batches across every API surface, newest first."""
    store = _require_store(store)
    rows = await store.list_batches(user_id=current_user["user_id"], limit=limit + 1, after=after)
    data = [batch_view(row) for row in rows[:limit]]
    return {
        "object": "list",
        "data": data,
        "has_more": len(rows) > limit,
        "last_id": data[-1]["id"] if data else None,
    }


@router.post("/user/batches/{batch_id}/cancel")
async def cancel_my_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
) -> dict[str, Any]:
    """Cancel one of the user's batches."""
    store = _require_store(store)
    row = await store.cancel_batch(batch_id, user_id=current_user["user_id"])
    if row is None:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    return batch_view(row)


@router.delete("/user/batches/{batch_id}")
async def delete_my_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
) -> dict[str, Any]:
    """Delete one of the user's batches with its results."""
    store = _require_store(store)
    try:
        deleted = await store.delete_batch(batch_id, user_id=current_user["user_id"])
    except BatchDeleteInProgress:
        raise HTTPException(
            409,
            "This batch has a running request; cancel it and wait for its running requests "
            "to finish before deleting it",
        ) from None
    if not deleted:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    return {"id": batch_id, "deleted": True}


@router.get("/user/batches/{batch_id}/results", response_model=None)
async def download_my_results(
    batch_id: str,
    kind: str = Query("all", pattern="^(all|output|error)$"),
    current_user: dict = Depends(get_current_user),
    store=Depends(get_batch_store),
) -> StreamingResponse:
    """Download a finished batch's results as JSONL, in its protocol's line shape."""
    store = _require_store(store)
    row = await store.get_batch(batch_id, user_id=current_user["user_id"])
    if row is None:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    if not results_final(row):
        raise HTTPException(409, "This batch has not finished; its results are not final")
    surface = row.get("api_surface") or "openai_inline"
    if surface != SURFACE_OPENAI_FILE:
        kind = "all"
    return StreamingResponse(
        stream_lines(store=store, batch_id=batch_id, surface=surface, kind=kind),
        media_type="application/jsonl",
        headers={"content-disposition": content_disposition(f"{batch_id}_{kind}.jsonl")},
    )


# -- webhooks -----------------------------------------------------------------------


class WebhookSettings(BaseModel):
    """A webhook to create or change."""

    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    rotate_secret: bool = False


def _webhook_view(row: dict[str, Any] | None, *, secret: str | None = None) -> dict[str, Any]:
    if row is None:
        return {"configured": False}
    view = {
        "configured": True,
        "url": row["url"],
        "enabled": bool(row["enabled"]),
        "created_at": _epoch(row.get("created_at")),
        "updated_at": _epoch(row.get("updated_at")),
    }
    if secret is not None:
        view["secret"] = secret
    return view


def _require_webhooks(webhook_store: Any) -> Any:
    if webhook_store is None or not batch_config().enabled:
        raise HTTPException(404, _UNAVAILABLE)
    return webhook_store


async def _save_webhook(
    webhook_store: Any,
    user_id: str,
    settings: WebhookSettings,
    *,
    role: str | None,
    check_owner: bool = True,
) -> dict:
    config = batch_config()
    # Before the URL is resolved or the store read: a caller who may not
    # create batches learns nothing and changes nothing here.
    if not may_use_batch(role, config):
        raise HTTPException(
            403, f"Configuring a batch webhook requires the {config.min_role!r} role or higher"
        )
    url, problem = url_problem(settings.url, allow_private=config.webhook_allow_private)
    if problem is None and url is not None:
        problem = await check_resolves_public(url, allow_private=config.webhook_allow_private)
    if problem is not None or url is None:
        raise HTTPException(400, f"url: {problem or 'The webhook URL is not allowed.'}")
    existing = await webhook_store.get_webhook(user_id)
    secret = None
    if existing is None or settings.rotate_secret:
        secret = new_secret()
    try:
        row = await webhook_store.set_webhook(
            user_id=user_id,
            url=url,
            secret_encrypted=encrypt_api_key(secret) if secret is not None else None,
            enabled=settings.enabled,
            check_owner=check_owner,
        )
    except LookupError:
        # The account went away (or into erasure) while the URL was checked.
        raise HTTPException(401, "Invalid or expired credentials") from None
    return _webhook_view(row, secret=secret)


async def _events(webhook_store: Any, user_id: str) -> dict[str, Any]:
    rows = await webhook_store.list_events(user_id, limit=20)
    return {
        "object": "list",
        "data": [
            {
                "id": row["id"],
                "batch_id": row["batch_id"],
                "type": row["event_type"],
                "status": row["status"],
                "attempts": row["attempts"],
                "last_status_code": row["last_status_code"],
                "last_error": row["last_error"],
                "created_at": _epoch(row["created_at"]),
                "delivered_at": _epoch(row["delivered_at"]),
                "next_attempt_at": _epoch(row["next_attempt_at"]),
            }
            for row in rows
        ],
    }


@router.get("/user/batch-webhook")
async def get_my_webhook(
    current_user: dict = Depends(get_current_user),
    webhook_store=Depends(get_batch_webhook_store),
) -> dict[str, Any]:
    """The user's batch webhook (never its secret)."""
    webhook_store = _require_webhooks(webhook_store)
    return _webhook_view(await webhook_store.get_webhook(current_user["user_id"]))


@router.put("/user/batch-webhook")
async def put_my_webhook(
    settings: WebhookSettings,
    current_user: dict = Depends(get_current_user),
    webhook_store=Depends(get_batch_webhook_store),
) -> dict[str, Any]:
    """Create or change the user's webhook; a new or rotated secret is returned once."""
    webhook_store = _require_webhooks(webhook_store)
    return await _save_webhook(
        webhook_store, current_user["user_id"], settings, role=current_user.get("role")
    )


@router.delete("/user/batch-webhook")
async def delete_my_webhook(
    current_user: dict = Depends(get_current_user),
    webhook_store=Depends(get_batch_webhook_store),
) -> dict[str, Any]:
    """Remove the user's webhook and its undelivered events."""
    webhook_store = _require_webhooks(webhook_store)
    return {"deleted": await webhook_store.delete_webhook(current_user["user_id"])}


@router.get("/user/batch-webhook/events")
async def my_webhook_events(
    current_user: dict = Depends(get_current_user),
    webhook_store=Depends(get_batch_webhook_store),
) -> dict[str, Any]:
    """The user's recent webhook events and how their delivery went."""
    webhook_store = _require_webhooks(webhook_store)
    return await _events(webhook_store, current_user["user_id"])


def _key_owner(user_ctx: dict[str, Any]) -> str:
    return user_ctx.get("user_id") or "anonymous"


@router.get("/v1/batch_webhook")
async def get_webhook(
    user_ctx: dict = Depends(verify_batch_management_identity),
    webhook_store=Depends(get_batch_webhook_store),
) -> dict[str, Any]:
    """The caller's batch webhook (never its secret)."""
    webhook_store = _require_webhooks(webhook_store)
    return _webhook_view(await webhook_store.get_webhook(_key_owner(user_ctx)))


@router.put("/v1/batch_webhook")
async def put_webhook(
    settings: WebhookSettings,
    user_ctx: dict = Depends(verify_batch_management_identity),
    webhook_store=Depends(get_batch_webhook_store),
) -> dict[str, Any]:
    """Create or change the caller's webhook; a new or rotated secret is returned once."""
    webhook_store = _require_webhooks(webhook_store)
    return await _save_webhook(
        webhook_store,
        _key_owner(user_ctx),
        settings,
        role=user_ctx.get("role"),
        check_owner=bool(user_ctx.get("authenticated")),
    )


@router.delete("/v1/batch_webhook")
async def delete_webhook(
    user_ctx: dict = Depends(verify_batch_management_identity),
    webhook_store=Depends(get_batch_webhook_store),
) -> dict[str, Any]:
    """Remove the caller's webhook and its undelivered events."""
    webhook_store = _require_webhooks(webhook_store)
    return {"deleted": await webhook_store.delete_webhook(_key_owner(user_ctx))}


@router.get("/v1/batch_webhook/events")
async def webhook_events(
    user_ctx: dict = Depends(verify_batch_management_identity),
    webhook_store=Depends(get_batch_webhook_store),
) -> dict[str, Any]:
    """The caller's recent webhook events and how their delivery went."""
    webhook_store = _require_webhooks(webhook_store)
    return await _events(webhook_store, _key_owner(user_ctx))
