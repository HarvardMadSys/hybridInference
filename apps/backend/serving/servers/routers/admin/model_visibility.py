"""Admin model visibility endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serving.audit import AuditActor, AuditTarget
from serving.config.settings import VALID_ROLES
from serving.model_access import baseline_required_role
from serving.schemas_admin import (
    ListModelVisibilityResponse,
    ModelVisibilityItem,
    UpdateModelVisibilityRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_admin_audit_actor,
    get_operational_store,
    get_services,
    model_router_transition_lock,
    verify_admin_access,
)

router = APIRouter(prefix="/admin")


def _is_canonical_model(model_id: str, route) -> bool:
    return (
        getattr(route, "published", True)
        and bool(route.adapters)
        and route.adapters[0][0].config.id == model_id
    )


def _model_routes(router) -> dict[str, object]:
    """Return chat and embedding gates keyed by their registered names."""
    return {
        **getattr(router, "routes", {}),
        **getattr(router, "embedding_routes", {}),
    }


def _effective_required_role_for_admin_list(baseline: str, override: str | None) -> str:
    if override is None:
        return baseline
    if override not in VALID_ROLES:
        return "admin"
    return override


@router.get("/models/visibility", response_model=ListModelVisibilityResponse)
async def list_model_visibility(
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ListModelVisibilityResponse:
    """List baseline, override, and effective required_role for canonical models."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    override_rows = await op_store.list_model_visibility_overrides()
    overrides = {row["model_id"]: row["required_role"] for row in override_rows}
    models: list[ModelVisibilityItem] = []

    routes = _model_routes(services.router)
    for model_id in sorted(routes):
        route = routes[model_id]
        if not _is_canonical_model(model_id, route):
            continue

        baseline = baseline_required_role(route)
        override = overrides.get(model_id)
        effective = _effective_required_role_for_admin_list(baseline, override)
        models.append(
            ModelVisibilityItem(
                model_id=model_id,
                baseline_required_role=baseline,
                override_required_role=override,
                effective_required_role=effective,
            )
        )

    return ListModelVisibilityResponse(models=models)


@router.patch("/models/{model_id:path}/visibility", response_model=ModelVisibilityItem)
async def update_model_visibility(
    model_id: str,
    payload: UpdateModelVisibilityRequest,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ModelVisibilityItem:
    """Set or clear a per-model required_role override."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    async with model_router_transition_lock(services, model_id):
        route = _model_routes(services.router).get(model_id)
        if route is None or not _is_canonical_model(model_id, route):
            raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")

        baseline = baseline_required_role(route)
        old_row = await op_store.get_model_visibility_override(model_id)
        old_override = None if old_row is None else old_row.get("required_role")

        resolver = services.model_visibility_resolver
        try:
            if payload.required_role is None:
                await op_store.delete_model_visibility_override(model_id)
                new_override = None
            else:
                if payload.required_role not in VALID_ROLES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid required_role: {payload.required_role}",
                    )
                await op_store.set_model_visibility_override(
                    model_id, payload.required_role, admin_id
                )
                new_override = payload.required_role
        finally:
            # A write may commit before its transport surfaces an error. Fence
            # any pre-existing or in-flight role read regardless of outcome.
            if resolver is not None:
                resolver.invalidate_model(model_id)

        new_effective = baseline
        if resolver is not None:
            new_effective = await resolver.get_effective_required_role(model_id, baseline)
        elif new_override is not None:
            new_effective = new_override

        await log_admin_action(
            op_store,
            action="models.visibility.update",
            actor=actor,
            target=AuditTarget.model(model_id),
            details={
                "model_id": model_id,
                "old_override_required_role": old_override,
                "new_override_required_role": new_override,
                "old_effective_required_role": _effective_required_role_for_admin_list(
                    baseline, old_override
                ),
                "new_effective_required_role": new_effective,
            },
        )

        return ModelVisibilityItem(
            model_id=model_id,
            baseline_required_role=baseline,
            override_required_role=new_override,
            effective_required_role=new_effective,
        )
