"""Admin endpoints for the per-model batch concurrency caps.

An administrator caps how many batch requests each model runs at once across
the whole deployment: the worker enforces the cap in the query that claims
work (``serving.batch.scheduler``), however many gateway processes run. A model
without a cap of its own takes ``BATCH_MODEL_CONCURRENCY``, set with the other
batch settings in Admin -> Settings -> Batch; a cap of 0 pauses
batch work on the model. Every process reads the caps through
``BatchModelLimits``: the process that serves a write sees it at once, the
others within seconds.

It also gives administrators an overview of every user's batches
(``/admin/batch/jobs``, ``/admin/batch/summary``) and lets them cancel one.
"""

from __future__ import annotations

from typing import Any, get_args

from fastapi import APIRouter, Depends, HTTPException, Query

from serving.audit import AuditActor, AuditTarget
from serving.batch.config import batch_config
from serving.batch.schemas import BatchStatus
from serving.schemas_admin import (
    BatchModelLimitItem,
    ListBatchModelLimitsResponse,
    UpdateBatchModelLimitRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_admin_audit_actor,
    get_batch_model_limits,
    get_batch_store,
    get_operational_store,
    get_services,
    model_router_transition_lock,
    verify_admin_access,
)

router = APIRouter(prefix="/admin")

#: The statuses a batch shows; the overview filters on these, not the stored ones.
_STATUS_PATTERN = f"^({'|'.join(get_args(BatchStatus))})$"


def _is_canonical_model(model_id: str, route: Any) -> bool:
    return (
        getattr(route, "published", True)
        and bool(route.adapters)
        and route.adapters[0][0].config.id == model_id
    )


def _item(
    model_id: str,
    override: int | None,
    default: int,
    stats: dict[str, int] | None,
) -> BatchModelLimitItem:
    stats = stats or {}
    return BatchModelLimitItem(
        model_id=model_id,
        max_concurrency=override,
        effective_concurrency=default if override is None else override,
        running=int(stats.get("running", 0)),
        queued=int(stats.get("queued", 0)),
    )


@router.get("/batch/models", response_model=ListBatchModelLimitsResponse)
async def list_batch_model_limits(
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    batch_store=Depends(get_batch_store),
) -> ListBatchModelLimitsResponse:
    """List each model's batch concurrency cap and the batch requests it holds now.

    Canonical published chat models are listed, and so is any other model that
    still has a cap or open batch requests, such as one taken out of the route
    table while its batches wait.
    """
    if batch_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    default = batch_config().default_model_concurrency
    overrides = await batch_store.list_model_limits()
    stats = await batch_store.model_stats()
    model_ids = {
        model_id
        for model_id, route in services.router.routes.items()
        if _is_canonical_model(model_id, route)
    }
    model_ids.update(overrides, stats)

    return ListBatchModelLimitsResponse(
        default_concurrency=default,
        models=[
            _item(model_id, overrides.get(model_id), default, stats.get(model_id))
            for model_id in sorted(model_ids)
        ],
    )


@router.patch("/batch/models/{model_id:path}", response_model=BatchModelLimitItem)
async def update_batch_model_limit(
    model_id: str,
    payload: UpdateBatchModelLimitRequest,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
    batch_store=Depends(get_batch_store),
    limits=Depends(get_batch_model_limits),
) -> BatchModelLimitItem:
    """Set a model's batch concurrency cap, or clear it (null) to use the default.

    Setting a cap needs a canonical published chat model. Clearing one also
    works for a model that has left the route table with its cap still stored.
    """
    if batch_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    new = payload.max_concurrency
    async with model_router_transition_lock(services, model_id):
        route = services.router.routes.get(model_id)
        routed = route is not None and _is_canonical_model(model_id, route)
        if not routed and new is not None:
            raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")
        old = (await batch_store.list_model_limits()).get(model_id)
        if not routed and old is None:
            # Not routed, and no cap to clear.
            raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")

        try:
            if new is None:
                await batch_store.delete_model_limit(model_id)
            else:
                await batch_store.set_model_limit(model_id, new, admin_id)
        finally:
            # Fence stale cap reads even when a committed write reports an
            # ambiguous transport failure.
            if limits is not None:
                limits.invalidate()

        await log_admin_action(
            op_store,
            action="batch.models.update",
            actor=actor,
            target=AuditTarget.model(model_id),
            details={
                "model_id": model_id,
                "old_max_concurrency": old,
                "new_max_concurrency": new,
            },
        )

    stats = await batch_store.model_stats()
    return _item(model_id, new, batch_config().default_model_concurrency, stats.get(model_id))


# -- every user's batches -------------------------------------------------------------


@router.get("/batch/jobs")
async def list_all_batches(
    user_id: str | None = Query(None, description="Only this user's batches"),
    status: str | None = Query(
        None,
        pattern=_STATUS_PATTERN,
        description="Only batches showing this status (the one listed with them)",
    ),
    limit: int = Query(50, ge=1, le=200),
    after: str | None = Query(None),
    _admin_id: str = Depends(verify_admin_access),
    batch_store=Depends(get_batch_store),
) -> dict[str, Any]:
    """Every user's batches, newest first, with counts, tokens and known cost."""
    if batch_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    from serving.servers.routers.batch_console import batch_view

    rows = await batch_store.list_batches(
        user_id=user_id, limit=limit + 1, after=after, status=status
    )
    data = [batch_view(row) for row in rows[:limit]]
    return {
        "object": "list",
        "data": data,
        "has_more": len(rows) > limit,
        "last_id": data[-1]["id"] if data else None,
    }


@router.get("/batch/summary")
async def batch_summary(
    _admin_id: str = Depends(verify_admin_access),
    batch_store=Depends(get_batch_store),
) -> dict[str, Any]:
    """Per-user batch totals: batches, active ones, requests, tokens and known cost."""
    if batch_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    users = await batch_store.batch_summary()
    for user in users:
        created = user.get("last_created_at")
        user["last_created_at"] = int(created.timestamp()) if created is not None else None
    totals = {
        key: sum(user[key] for user in users)
        for key in (
            "batches",
            "active",
            "requests",
            "completed",
            "failed",
            "prompt_tokens",
            "completion_tokens",
            "unknown_cost_attempts",
        )
    }
    totals["cost_usd"] = sum(user["cost_usd"] for user in users)
    return {"users": users, "totals": totals}


@router.post("/batch/jobs/{batch_id}/cancel")
async def admin_cancel_batch(
    batch_id: str,
    _admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
    batch_store=Depends(get_batch_store),
) -> dict[str, Any]:
    """Cancel any user's batch (audited as ``batch.jobs.cancel``)."""
    if batch_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    from serving.servers.routers.batch_console import batch_view

    row = await batch_store.cancel_batch(batch_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    # The batch's owner is the affected user, so the row is about them (and
    # their erasure takes it); which batch is in the details.
    await log_admin_action(
        op_store,
        action="batch.jobs.cancel",
        actor=actor,
        target=AuditTarget.user(row["user_id"]),
        details={"batch_id": batch_id},
    )
    return batch_view(row)
