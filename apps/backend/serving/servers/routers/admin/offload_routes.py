"""Admin endpoints for per-model queue-offload routes (see ``routing.offload``).

An admin designates one route of a fixed-routed model as its *offload route*
and sets how long a request may wait -- for an outbound concurrency slot, then
for its engine's first token -- before it is sent there. The offload route then
takes no ordinary traffic: ``FixedRouter`` reserves it for requests whose
selected route kept them waiting past the wait, and for requests no other route
could serve. Each request is judged on its own wait (``routing.engine_wait``):
nothing marks a route that kept one waiting, and it takes the next as usual.

Each policy is one ``site_settings`` row, applied to routing through
``OffloadRouteResolver``'s in-process snapshot the moment the write succeeds.
The route is named by its route id, the identity that survives an edit moving
the route to another host, so a retargeted route stays the offload route.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from routing.endpoints import endpoint_id_for_adapter, route_id_for_adapter
from routing.offload import OffloadPolicy
from serving.adapters.upstream_limiter import get_upstream_limiter
from serving.config.offload_routes import (
    OFFLOAD_ROUTE_VALUE_TYPE,
    OffloadRouteRecord,
    OffloadRouteResolver,
    encode_offload_policy,
    offload_route_setting_key,
)
from serving.schemas_admin import (
    ListOffloadRoutesResponse,
    ModelOffloadRouteResponse,
    OffloadRouteItem,
    UpdateOffloadRouteRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_operational_store,
    get_services,
    model_router_transition_lock,
    verify_admin_access,
)

router = APIRouter(prefix="/admin")


def _is_canonical_model(model_id: str, route: Any) -> bool:
    return (
        getattr(route, "published", True)
        and bool(route.adapters)
        and route.adapters[0][0].config.id == model_id
    )


def _raw_route_entries(route: Any) -> list[tuple[Any, float, str]]:
    raw_adapters = getattr(route, "raw_adapters", None)
    if raw_adapters:
        return list(raw_adapters)
    return [
        (adapter, float(weight), endpoint_id_for_adapter(adapter))
        for adapter, weight in route.adapters
    ]


def _canonical_route(services: Any, model_id: str) -> Any | None:
    route = services.router.routes.get(model_id)
    if route is None or not _is_canonical_model(model_id, route):
        return None
    return route


def _require_canonical_route(services: Any, model_id: str) -> Any:
    route = _canonical_route(services, model_id)
    if route is None:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")
    return route


def _require_resolver(services: Any) -> OffloadRouteResolver:
    resolver = getattr(services, "offload_route_resolver", None)
    if resolver is None:
        raise HTTPException(status_code=500, detail="Offload routes are not configured")
    return resolver


def _entries_for_route_id(route: Any, route_id: str) -> list[tuple[Any, float, str]]:
    return [
        entry for entry in _raw_route_entries(route) if route_id_for_adapter(entry[0]) == route_id
    ]


def _router_blocker(services: Any, model_id: str) -> str | None:
    """Return why the model's router would not apply an offload route, or None.

    Offload is a behavior of the shared ``FixedRouter``. RouteWise plans its own
    candidates, and the opt-in hybrid composition drives the fixed router one
    planned attempt at a time, so neither walks on to an offload route.
    """
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        return None
    strategy = registry.get_router_name(model_id)
    if strategy != "fixed":
        return f"The model uses the {strategy} routing policy; offload routes need fixed routing"
    if registry.get_router(model_id) is not services.router:
        return "The model uses hybrid composition, which does not apply offload routes"
    return None


def _route_is_weighted(
    services: Any,
    model_id: str,
    adapter: Any,
    raw_weight: float,
    endpoint_id: str,
) -> bool:
    """Return whether routing gives the route a weight above zero.

    Mirrors the two runtime rules ``FixedRouter`` applies on top of the model
    registry: a per-endpoint weight override, and the provider kill switch.
    Either one at zero means routing sends the route nothing, offloads included.
    """
    disabled = getattr(services, "disabled_provider_resolver", None)
    if disabled is not None and disabled.is_disabled(adapter.config.provider):
        return False
    weight = float(raw_weight)
    overrides = getattr(services, "weight_override_resolver", None)
    get_snapshot = getattr(overrides, "get_snapshot_for_model", None)
    if callable(get_snapshot):
        weight = float(get_snapshot(model_id).get(endpoint_id, weight))
    return weight > 0


def _offload_item(services: Any, model_id: str, record: OffloadRouteRecord) -> OffloadRouteItem:
    """Describe one stored policy and whether routing currently applies it."""
    policy = record.policy
    endpoint_id: str | None = None
    reason: str | None = None
    route = _canonical_route(services, model_id)
    if route is None:
        reason = "The model no longer exists"
    else:
        try:
            reason = _router_blocker(services, model_id)
        except Exception:
            # One model whose router cannot be resolved must not take the whole
            # list down with it; report it instead.
            reason = "The model's router could not be resolved"
        entries = _entries_for_route_id(route, policy.route_id)
        if not entries:
            reason = reason or "The route no longer exists"
        else:
            adapter, raw_weight, endpoint_id = entries[0]
            if reason is None and not _route_is_weighted(
                services, model_id, adapter, raw_weight, endpoint_id
            ):
                reason = (
                    "The route's effective weight is 0, so routing sends it nothing; "
                    "give it a weight above 0 (it still takes no ordinary traffic)"
                )
    return OffloadRouteItem(
        model_id=model_id,
        route_id=policy.route_id,
        wait_seconds=policy.wait_seconds,
        endpoint_id=endpoint_id,
        active=reason is None,
        inactive_reason=reason,
        updated_at=record.updated_at,
        updated_by=record.updated_by,
    )


def _model_response(
    services: Any,
    model_id: str,
    record: OffloadRouteRecord | None,
) -> ModelOffloadRouteResponse:
    return ModelOffloadRouteResponse(
        model_id=model_id,
        offload=_offload_item(services, model_id, record) if record is not None else None,
    )


@router.get("/routing/offload-routes", response_model=ListOffloadRoutesResponse)
async def list_offload_routes(
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ListOffloadRoutesResponse:
    """List every model's offload route and the queue they are measured against."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    resolver = _require_resolver(services)
    limiter = get_upstream_limiter()
    records = resolver.list_records()
    return ListOffloadRoutesResponse(
        queue_enabled=limiter.enabled,
        max_wait_seconds=limiter.acquire_timeout,
        offload_routes=[
            _offload_item(services, model_id, records[model_id]) for model_id in sorted(records)
        ],
    )


@router.get("/routing/offload-routes/{model_id:path}", response_model=ModelOffloadRouteResponse)
async def get_offload_route(
    model_id: str,
    _admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ModelOffloadRouteResponse:
    """Return one model's offload route, or ``offload: null`` when it has none."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    resolver = _require_resolver(services)
    record = resolver.get_record(model_id)
    if record is None:
        # A stored policy is reported even for a model that has since gone, so
        # it can be found and cleared; with no policy, an unknown id is a 404.
        _require_canonical_route(services, model_id)
    return _model_response(services, model_id, record)


@router.put("/routing/offload-routes/{model_id:path}", response_model=ModelOffloadRouteResponse)
async def set_offload_route(
    model_id: str,
    payload: UpdateOffloadRouteRequest,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ModelOffloadRouteResponse:
    """Designate one of a model's routes as its offload route."""
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    resolver = _require_resolver(services)
    # No ceiling past positive and finite (the request model's own checks). A
    # wait longer than the limiter's acquire timeout still ends a queue wait at
    # that timeout, which offloads the request too; what it lengthens is the
    # engine's first-token wait, which a model whose long prompts take a while
    # to start answering needs (``routing.engine_wait``).
    _require_canonical_route(services, model_id)

    async with model_router_transition_lock(services, model_id):
        route = _require_canonical_route(services, model_id)
        blocker = _router_blocker(services, model_id)
        if blocker is not None:
            raise HTTPException(status_code=422, detail=blocker)
        entries = _raw_route_entries(route)
        matches = _entries_for_route_id(route, payload.route_id)
        if not matches:
            raise HTTPException(status_code=400, detail="unknown route id")
        if len(matches) > 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"duplicate provider route id {payload.route_id!r}; set unique "
                    "endpoint_id values before choosing an offload route"
                ),
            )
        if len(entries) < 2:
            raise HTTPException(
                status_code=422,
                detail=(
                    "An offload route takes no ordinary traffic, so the model needs at least "
                    "one other route to offload from"
                ),
            )
        _adapter, _weight, endpoint_id = matches[0]
        policy = OffloadPolicy(route_id=payload.route_id, wait_seconds=payload.wait_seconds)
        previous = resolver.get_record(model_id)
        try:
            await op_store.set_setting(
                offload_route_setting_key(model_id),
                encode_offload_policy(policy),
                OFFLOAD_ROUTE_VALUE_TYPE,
                admin_id,
            )
        except BaseException:
            # The write may have committed before the transport failed. Route
            # without an offload until the next reload reads the durable value,
            # rather than on a snapshot that may disagree with it.
            resolver.clear_model(model_id)
            raise
        resolver.set_policy(
            model_id,
            policy,
            updated_by=admin_id,
            updated_at=datetime.now(timezone.utc),
        )
        await log_admin_action(
            op_store,
            admin_id,
            "routing.offload_routes.update",
            None,
            {
                "model_id": model_id,
                "route_id": policy.route_id,
                "endpoint_id": endpoint_id,
                "wait_seconds": policy.wait_seconds,
                "old_route_id": previous.policy.route_id if previous is not None else None,
                "old_wait_seconds": (
                    previous.policy.wait_seconds if previous is not None else None
                ),
            },
        )
        return _model_response(services, model_id, resolver.get_record(model_id))


@router.delete(
    "/routing/offload-routes/{model_id:path}",
    response_model=ModelOffloadRouteResponse,
)
async def clear_offload_route(
    model_id: str,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> ModelOffloadRouteResponse:
    """Clear a model's offload route; its route returns to ordinary selection.

    Idempotent, and deliberately not gated on the model still existing: a policy
    left behind by a model that was removed from the registry has to be
    clearable too.
    """
    if op_store is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    resolver = _require_resolver(services)

    async with model_router_transition_lock(services, model_id):
        previous = resolver.get_record(model_id)
        try:
            removed = await op_store.delete_setting(offload_route_setting_key(model_id))
        except BaseException:
            resolver.clear_model(model_id)
            raise
        resolver.clear_model(model_id)
        if previous is not None or removed:
            await log_admin_action(
                op_store,
                admin_id,
                "routing.offload_routes.clear",
                None,
                {
                    "model_id": model_id,
                    "old_route_id": previous.policy.route_id if previous is not None else None,
                    "old_wait_seconds": (
                        previous.policy.wait_seconds if previous is not None else None
                    ),
                },
            )
        return ModelOffloadRouteResponse(model_id=model_id, offload=None)
