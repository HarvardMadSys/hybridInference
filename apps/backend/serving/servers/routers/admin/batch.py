"""Admin endpoints for the per-model batch concurrency caps.

An administrator caps how many batch requests each model runs at once across
the whole deployment: the worker enforces the cap in the query that claims
work (``serving.batch.scheduler``), however many gateway processes run. A model
without a cap of its own takes ``BATCH_MODEL_CONCURRENCY``, set with the other
batch settings in Admin -> Configuration -> Batch inference; a cap of 0 pauses
batch work on the model. Every process reads the caps through
``BatchModelLimits``: the process that serves a write sees it at once, the
others within seconds.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.batch.config import batch_config
from serving.schemas_admin import (
    BatchModelLimitItem,
    ListBatchModelLimitsResponse,
    UpdateBatchModelLimitRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_batch_model_limits,
    get_batch_store,
    get_operational_store,
    get_services,
    model_router_transition_lock,
    verify_admin_access,
)
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")


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
    request: Request,
    model_id: str,
    payload: UpdateBatchModelLimitRequest,
    admin_id: str = Depends(verify_admin_access),
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
            get_client_ip(request),
            "batch.models.update",
            None,
            {
                "model_id": model_id,
                "old_max_concurrency": old,
                "new_max_concurrency": new,
            },
        )

    stats = await batch_store.model_stats()
    return _item(model_id, new, batch_config().default_model_concurrency, stats.get(model_id))
