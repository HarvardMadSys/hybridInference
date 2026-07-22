"""Admin model concurrency-exemption endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.schemas_admin import (
    ListModelConcurrencyResponse,
    ModelConcurrencyItem,
    UpdateModelConcurrencyRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_operational_store,
    get_services,
    model_router_transition_lock,
    verify_admin_access,
)
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")


def _is_canonical_model(model_id: str, route) -> bool:
    return (
        getattr(route, "published", True)
        and bool(route.adapters)
        and route.adapters[0][0].config.id == model_id
    )


def _canonical_embedding_ids(services) -> list[str]:
    adapters = getattr(services, "embedding_adapters", None) or {}
    by_identity: dict[int, str] = {}
    for adapter in adapters.values():
        cid = getattr(getattr(adapter, "config", None), "id", None)
        if cid:
            by_identity[id(adapter)] = cid
    return list(by_identity.values())


@router.get("/models/concurrency", response_model=ListModelConcurrencyResponse)
async def list_model_concurrency(
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ListModelConcurrencyResponse:
    """List per-user concurrency-limit exemption state for canonical models."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    exemption_rows = await op_store.list_model_concurrency_exemptions()
    exempt_ids = {row["model_id"] for row in exemption_rows}
    models: list[ModelConcurrencyItem] = []

    for model_id in sorted(services.router.routes):
        route = services.router.routes[model_id]
        if not _is_canonical_model(model_id, route):
            continue

        models.append(
            ModelConcurrencyItem(
                model_id=model_id,
                exempt=model_id in exempt_ids,
            )
        )

    existing = {m.model_id for m in models}
    for cid in _canonical_embedding_ids(services):
        if cid not in existing:
            models.append(ModelConcurrencyItem(model_id=cid, exempt=cid in exempt_ids))
    models.sort(key=lambda m: m.model_id)

    return ListModelConcurrencyResponse(models=models)


@router.patch("/models/{model_id:path}/concurrency", response_model=ModelConcurrencyItem)
async def update_model_concurrency(
    request: Request,
    model_id: str,
    payload: UpdateModelConcurrencyRequest,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ModelConcurrencyItem:
    """Set or clear a per-model concurrency-limit exemption."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    async with model_router_transition_lock(services, model_id):
        route = services.router.routes.get(model_id)
        is_chat = route is not None and _is_canonical_model(model_id, route)
        is_embedding = model_id in set(_canonical_embedding_ids(services))
        if not (is_chat or is_embedding):
            raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")

        old_row = await op_store.get_model_concurrency_exemption(model_id)
        old_exempt = old_row is not None

        resolver = services.model_concurrency_resolver
        try:
            if payload.exempt:
                await op_store.set_model_concurrency_exemption(model_id, admin_id)
            else:
                await op_store.delete_model_concurrency_exemption(model_id)
            new_exempt = payload.exempt
        finally:
            # Fence stale exemption reads even when a committed write reports
            # an ambiguous transport failure.
            if resolver is not None:
                resolver.invalidate_model(model_id)

        await log_admin_action(
            op_store,
            get_client_ip(request),
            "models.concurrency.update",
            None,
            {
                "model_id": model_id,
                "old_exempt": old_exempt,
                "new_exempt": new_exempt,
            },
        )

        return ModelConcurrencyItem(model_id=model_id, exempt=new_exempt)
