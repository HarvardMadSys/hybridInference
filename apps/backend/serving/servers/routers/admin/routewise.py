"""Dedicated admin Routewise runtime settings endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from routing.routewise.router import RouteWiseRouter
from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    get_runtime_settings,
)
from serving.schemas_admin import (
    ListRoutewiseSettingsResponse,
    RoutewiseSettingItem,
    UpdateSettingRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin/routewise")

ROUTEWISE_KEYS = (
    "routewise_decision_rule",
    "routewise_daily_quota",
    "routewise_latency_slo_sec",
    "routewise_latency_min_samples",
)

ROUTEWISE_DECISION_RULES = {"pd", "lapd"}


def _require_runtime_settings(rt: RuntimeSettings | None) -> RuntimeSettings:
    """Return the singleton or raise 503 if the app hasn't initialized it yet."""
    if rt is None:
        raise HTTPException(status_code=503, detail="Runtime settings not initialized")
    return rt


def _serialize_existing_value(raw: str | None, expected_type: str) -> Any:
    if expected_type == "bool":
        return raw.lower() in ("true", "1", "yes") if isinstance(raw, str) else raw
    if expected_type == "int":
        return int(raw) if raw is not None else None
    if expected_type == "float":
        return float(raw) if raw is not None else None
    return raw


async def _refresh_live_routewise_routers(request: Request, rt: RuntimeSettings) -> None:
    """Refresh cached RouteWise router instances from current runtime settings."""
    services = getattr(request.app.state, "services", None)
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        return

    decision_rule = await rt.get_str("routewise_decision_rule")
    daily_quota = await rt.get_int("routewise_daily_quota")
    latency_slo_sec = await rt.get_float("routewise_latency_slo_sec")
    latency_min_samples = await rt.get_int("routewise_latency_min_samples")

    for router in registry._cache.values():
        if isinstance(router, RouteWiseRouter):
            router.apply_runtime_overrides(
                decision_rule=decision_rule,
                daily_quota=daily_quota,
                latency_slo_sec=latency_slo_sec,
                latency_min_samples=latency_min_samples,
            )


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

    if key == "routewise_decision_rule" and value not in ROUTEWISE_DECISION_RULES:
        allowed = ", ".join(sorted(ROUTEWISE_DECISION_RULES))
        raise HTTPException(
            status_code=400,
            detail=f"Setting '{key}' must be one of: {allowed}",
        )

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
    rt.invalidate_key(key)
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
