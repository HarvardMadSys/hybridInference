"""Dedicated admin Routewise runtime settings endpoints."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from routing.routewise.router import RouteWiseRouter
from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    get_runtime_settings,
)
from serving.schemas_admin import (
    ListRoutewiseProbeSamplesResponse,
    ListRoutewiseSettingsResponse,
    RoutewiseProbeRunResult,
    RoutewiseProbeSampleItem,
    RoutewiseSettingItem,
    RunRoutewiseProbeRequest,
    RunRoutewiseProbeResponse,
    UpdateSettingRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, get_services, verify_admin_access
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin/routewise")

ROUTEWISE_KEYS = (
    "routewise_budget_alpha",
    "routewise_latency_slo_sec",
    "routewise_latency_min_samples",
    "routewise_probe_enabled",
    "routewise_probe_interval_sec",
)


def _require_runtime_settings(rt: RuntimeSettings | None) -> RuntimeSettings:
    """Return the singleton or raise 503 if the app hasn't initialized it yet."""
    if rt is None:
        raise HTTPException(status_code=503, detail="Runtime settings not initialized")
    return rt


def _serialize_existing_value(raw: str | None, expected_type: str) -> Any:
    if raw is None:
        return None
    if expected_type == "bool":
        return raw.lower() in ("true", "1", "yes") if isinstance(raw, str) else raw
    try:
        if expected_type == "int":
            return int(raw)
        if expected_type == "float":
            return float(raw)
    except (TypeError, ValueError):
        return raw
    return raw


def _routewise_routers_for_probe(services: Any, model_id: str | None) -> list[RouteWiseRouter]:
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        return []
    if model_id:
        router_obj = registry.get_router(model_id)
        return [router_obj] if isinstance(router_obj, RouteWiseRouter) else []
    for configured_model_id in registry.configured_model_ids():
        if registry.get_router_name(configured_model_id) == "routewise":
            registry.get_router(configured_model_id)
    seen: set[int] = set()
    routers: list[RouteWiseRouter] = []
    for router_obj in registry.cached_routers():
        if isinstance(router_obj, RouteWiseRouter) and id(router_obj) not in seen:
            routers.append(router_obj)
            seen.add(id(router_obj))
    return routers


async def _refresh_live_routewise_routers(request: Request, rt: RuntimeSettings) -> None:
    """Refresh cached RouteWise router instances from current runtime settings."""
    services = getattr(request.app.state, "services", None)
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        return

    for key in ROUTEWISE_KEYS:
        rt.invalidate_key(key)

    budget_alpha = await rt.get_float("routewise_budget_alpha")
    latency_slo_sec = await rt.get_float("routewise_latency_slo_sec")
    latency_min_samples = await rt.get_int("routewise_latency_min_samples")
    routewise_probe_enabled = await rt.get_bool("routewise_probe_enabled")
    routewise_probe_interval_sec = await rt.get_float("routewise_probe_interval_sec")

    for model_id in registry.configured_model_ids():
        if registry.get_router_name(model_id) != "routewise":
            continue
        registry.get_router(model_id)

    for router in registry.cached_routers():
        if isinstance(router, RouteWiseRouter):
            router.apply_runtime_overrides(
                budget_alpha=budget_alpha,
                latency_slo_sec=latency_slo_sec,
                latency_min_samples=latency_min_samples,
                routewise_probe_enabled=routewise_probe_enabled,
                routewise_probe_interval_sec=routewise_probe_interval_sec,
            )
            await router.refresh_probe_task()


@router.get("/settings", response_model=ListRoutewiseSettingsResponse)
async def list_routewise_settings_endpoint(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> ListRoutewiseSettingsResponse:
    """List the curated Routewise runtime settings."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)

    items_by_key = {item["key"]: item for item in await rt.list_all()}
    return ListRoutewiseSettingsResponse(
        settings=[
            RoutewiseSettingItem(
                key=key,
                value=items_by_key[key]["value"],
                value_type=items_by_key[key]["value_type"],
                default_value=items_by_key[key]["default_value"],
                description=items_by_key[key]["description"],
                min=items_by_key[key].get("min"),
                max=items_by_key[key].get("max"),
            )
            for key in ROUTEWISE_KEYS
        ]
    )


@router.patch("/settings/{key}", response_model=RoutewiseSettingItem)
async def update_routewise_setting_endpoint(
    request: Request,
    key: str,
    payload: UpdateSettingRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RoutewiseSettingItem:
    """Update a single curated Routewise runtime setting by key."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)

    if key not in ROUTEWISE_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")

    entry = RUNTIME_SETTINGS_REGISTRY.get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")

    expected_type = entry["type"]
    value = payload.value
    if expected_type == "bool" and not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a boolean value")
    if expected_type == "int" and (not isinstance(value, int) or isinstance(value, bool)):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects an integer value")
    if expected_type == "float" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a numeric value")
    if expected_type == "str" and not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a string value")

    if expected_type in ("int", "float"):
        lo = entry.get("min")
        hi = entry.get("max")
        if lo is not None and value < lo:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' value {value} is below min ({lo})",
            )
        if hi is not None and value > hi:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' value {value} is above max ({hi})",
            )

    old_row = await op_store.get_setting(key)
    if old_row is not None:
        old_value = _serialize_existing_value(old_row.get("value"), expected_type)
    else:
        from serving.config.settings import get_settings

        old_value = getattr(get_settings(), key, entry["default"])

    await op_store.set_setting(key, str(value), expected_type, admin_id)
    await _refresh_live_routewise_routers(request, rt)

    ip = get_client_ip(request)
    await log_admin_action(
        op_store,
        ip,
        "routewise_settings.update",
        None,
        {"key": key, "old_value": old_value, "new_value": value},
    )

    return RoutewiseSettingItem(
        key=key,
        value=value,
        value_type=expected_type,
        default_value=entry["default"],
        description=entry["description"],
        min=entry.get("min"),
        max=entry.get("max"),
    )


@router.get("/probes", response_model=ListRoutewiseProbeSamplesResponse)
async def list_routewise_probe_samples_endpoint(
    model_id: str | None = None,
    endpoint_id: str | None = None,
    since_seconds: int = 86_400,
    limit: int = 200,
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListRoutewiseProbeSamplesResponse:
    """List recent persisted RouteWise active-probe samples."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=max(int(since_seconds), 1))
    rows = await op_store.list_routewise_probe_samples(
        model_id=model_id,
        endpoint_id=endpoint_id,
        since=since,
        newest_first=True,
        limit=max(min(int(limit), 1000), 1),
    )
    return ListRoutewiseProbeSamplesResponse(
        samples=[
            RoutewiseProbeSampleItem(
                model_id=str(row["model_id"]),
                endpoint_id=str(row["endpoint_id"]),
                ttft_ms=(float(row["ttft_ms"]) if row.get("ttft_ms") is not None else None),
                ok=bool(row["ok"]),
                error=row.get("error"),
                checked_at=row["checked_at"],
            )
            for row in rows
        ]
    )


@router.post("/probes/run", response_model=RunRoutewiseProbeResponse)
async def run_routewise_probe_endpoint(
    request: Request,
    payload: RunRoutewiseProbeRequest,
    admin_id: str = Depends(verify_admin_access),
    services=Depends(get_services),
    op_store=Depends(get_operational_store),
) -> RunRoutewiseProbeResponse:
    """Manually run RouteWise latency probes against live route candidates."""
    routers = _routewise_routers_for_probe(services, payload.model_id)
    if not routers:
        raise HTTPException(status_code=404, detail="No RouteWise router found")
    results = []
    for router_obj in routers:
        attach_store = getattr(router_obj, "attach_operational_store", None)
        if callable(attach_store):
            attach_store(op_store)
        probe_model_id = payload.model_id
        canonical = getattr(router_obj, "_canonical_model_id", None)
        if callable(canonical) and probe_model_id:
            probe_model_id = canonical(probe_model_id)
        results.extend(
            await router_obj.run_probe_once(
                model_id=probe_model_id,
                endpoint_id=payload.endpoint_id,
                idle_only=payload.idle_only,
            )
        )
    # Audit after probing so failures still return probe diagnostics to the UI.
    await log_admin_action(
        op_store,
        get_client_ip(request),
        "routewise_probes.run",
        None,
        {
            "model_id": payload.model_id,
            "endpoint_id": payload.endpoint_id,
            "idle_only": payload.idle_only,
            "result_count": len(results),
            "admin_id": admin_id,
        },
    )
    return RunRoutewiseProbeResponse(
        results=[
            RoutewiseProbeRunResult(
                model_id=result.model_id,
                endpoint_id=result.endpoint_id,
                ok=result.ok,
                ttft_ms=result.ttft_ms,
                error=result.error,
            )
            for result in results
        ]
    )
