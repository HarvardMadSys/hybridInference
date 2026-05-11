"""Admin route weight override endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from routing.routers import _get_endpoint_id
from serving.schemas_admin import (
    ListAllRouteWeightsResponse,
    ListRouteWeightsResponse,
    RouteWeightItem,
    UpdateRouteWeightRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, get_services, verify_admin_access

router = APIRouter(prefix="/admin")


def _is_canonical_model(model_id: str, route) -> bool:
    return bool(route.adapters) and route.adapters[0][0].config.id == model_id


def _raw_route_entries(route) -> list[tuple[object, float, str]]:
    raw_adapters = getattr(route, "raw_adapters", None)
    if raw_adapters:
        return raw_adapters
    return [
        (adapter, float(weight), _get_endpoint_id(adapter)) for adapter, weight in route.adapters
    ]


async def _overrides_for_model(op_store, model_id: str) -> dict[str, float]:
    rows = await op_store.list_weight_overrides_for_model(model_id)
    return {str(row["endpoint_id"]): float(row["weight"]) for row in rows}


def _route_row(
    model_id: str, adapter, yaml_weight: float, override_weight: float | None
) -> RouteWeightItem:
    endpoint_id = _get_endpoint_id(adapter)
    return RouteWeightItem(
        model_id=model_id,
        endpoint_id=endpoint_id,
        provider=adapter.config.provider,
        base_url=getattr(adapter.config, "base_url", None),
        yaml_weight=float(yaml_weight),
        override_weight=override_weight,
        effective_weight=float(yaml_weight if override_weight is None else override_weight),
    )


def _validate_canonical_route(services, model_id: str):
    route = services.router.routes.get(model_id)
    if route is None or not _is_canonical_model(model_id, route):
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")
    return route


def _split_model_endpoint_path(services, model_endpoint_path: str):
    for model_id in sorted(services.router.routes, key=len, reverse=True):
        route = services.router.routes[model_id]
        prefix = f"{model_id}/"
        if _is_canonical_model(model_id, route) and model_endpoint_path.startswith(prefix):
            return model_id, model_endpoint_path[len(prefix) :], route
    raise HTTPException(status_code=404, detail="Unknown model")


def _entry_for_endpoint(route, endpoint_id: str):
    for adapter, yaml_weight, adapter_endpoint_id in _raw_route_entries(route):
        if adapter_endpoint_id == endpoint_id:
            return adapter, float(yaml_weight), adapter_endpoint_id
    raise HTTPException(status_code=400, detail="unknown endpoint for model")


async def _build_routes_for_model(model_id: str, route, op_store) -> list[RouteWeightItem]:
    overrides = await _overrides_for_model(op_store, model_id)
    return [
        _route_row(model_id, adapter, yaml_weight, overrides.get(endpoint_id))
        for adapter, yaml_weight, endpoint_id in _raw_route_entries(route)
    ]


def _set_weight_override_snapshot(
    services,
    model_id: str,
    endpoint_id: str,
    weight: float,
) -> None:
    resolver = getattr(services, "weight_override_resolver", None)
    if resolver is not None and hasattr(resolver, "set_override"):
        resolver.set_override(model_id, endpoint_id, weight)


def _clear_weight_override_snapshot(services, model_id: str, endpoint_id: str) -> None:
    resolver = getattr(services, "weight_override_resolver", None)
    if resolver is not None and hasattr(resolver, "clear_override"):
        resolver.clear_override(model_id, endpoint_id)


@router.get("/routing/weights/{model_id:path}", response_model=ListRouteWeightsResponse)
async def list_route_weights(
    model_id: str,
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ListRouteWeightsResponse:
    """List YAML, override, and effective route weights for one canonical model."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    route = _validate_canonical_route(services, model_id)
    return ListRouteWeightsResponse(
        model_id=model_id,
        routes=await _build_routes_for_model(model_id, route, op_store),
    )


@router.get("/routing/weights", response_model=ListAllRouteWeightsResponse)
async def list_all_route_weights(
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ListAllRouteWeightsResponse:
    """List YAML, override, and effective route weights for all canonical models."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    all_rows: list[RouteWeightItem] = []
    for model_id in sorted(services.router.routes):
        route = services.router.routes[model_id]
        if not _is_canonical_model(model_id, route):
            continue
        all_rows.extend(await _build_routes_for_model(model_id, route, op_store))
    return ListAllRouteWeightsResponse(routes=all_rows)


@router.put("/routing/weights/{model_endpoint_path:path}", response_model=RouteWeightItem)
async def set_route_weight(
    model_endpoint_path: str,
    payload: UpdateRouteWeightRequest,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> RouteWeightItem:
    """Set a runtime route weight override for one endpoint."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    if payload.weight < 0:
        raise HTTPException(status_code=400, detail="weight must be >= 0")

    model_id, endpoint_id, route = _split_model_endpoint_path(services, model_endpoint_path)
    adapter, yaml_weight, endpoint_id = _entry_for_endpoint(route, endpoint_id)
    overrides = await _overrides_for_model(op_store, model_id)
    effective = {
        adapter_endpoint_id: float(overrides.get(adapter_endpoint_id, raw_weight))
        for _, raw_weight, adapter_endpoint_id in _raw_route_entries(route)
    }
    old_override = overrides.get(endpoint_id)
    effective[endpoint_id] = float(payload.weight)
    if sum(effective.values()) <= 0:
        raise HTTPException(status_code=400, detail="cannot zero all routes for model")

    await op_store.upsert_weight_override(model_id, endpoint_id, float(payload.weight), admin_id)
    _set_weight_override_snapshot(services, model_id, endpoint_id, float(payload.weight))
    await log_admin_action(
        op_store,
        admin_id,
        "routing.weights.update",
        None,
        {
            "model_id": model_id,
            "endpoint_id": endpoint_id,
            "old_override_weight": old_override,
            "new_override_weight": float(payload.weight),
        },
    )
    return _route_row(model_id, adapter, yaml_weight, float(payload.weight))


@router.delete("/routing/weights/{model_endpoint_path:path}", response_model=RouteWeightItem)
async def clear_route_weight(
    model_endpoint_path: str,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> RouteWeightItem:
    """Clear a runtime route weight override, reverting to YAML weight."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    model_id, endpoint_id, route = _split_model_endpoint_path(services, model_endpoint_path)
    adapter, yaml_weight, endpoint_id = _entry_for_endpoint(route, endpoint_id)
    overrides = await _overrides_for_model(op_store, model_id)
    old_override = overrides.get(endpoint_id)
    await op_store.delete_weight_override(model_id, endpoint_id)
    _clear_weight_override_snapshot(services, model_id, endpoint_id)
    await log_admin_action(
        op_store,
        admin_id,
        "routing.weights.clear",
        None,
        {
            "model_id": model_id,
            "endpoint_id": endpoint_id,
            "old_override_weight": old_override,
        },
    )
    return _route_row(model_id, adapter, yaml_weight, None)
