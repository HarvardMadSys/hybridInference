"""Admin runtime settings endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    get_runtime_settings,
)
from serving.schemas_admin import (
    ListSettingsResponse,
    RuntimeSettingItem,
    UpdateSettingRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")

ROUTEWISE_SETTINGS_KEYS = {
    "routewise_daily_quota",
    "routewise_latency_slo_sec",
    "routewise_latency_min_samples",
}


def _require_runtime_settings(rt: RuntimeSettings | None) -> RuntimeSettings:
    """Return the singleton or raise 503 if the app hasn't initialized it yet."""
    if rt is None:
        raise HTTPException(status_code=503, detail="Runtime settings not initialized")
    return rt


@router.get("/settings", response_model=ListSettingsResponse)
async def list_runtime_settings_endpoint(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> ListSettingsResponse:
    """List all runtime settings with current values and defaults."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)
    items = await rt.list_all()
    return ListSettingsResponse(
        settings=[
            RuntimeSettingItem(
                key=i["key"],
                value=i["value"],
                value_type=i["value_type"],
                default_value=i["default_value"],
                description=i["description"],
                min=i.get("min"),
                max=i.get("max"),
            )
            for i in items
        ]
    )


@router.patch("/settings/{key}", response_model=RuntimeSettingItem)
async def update_runtime_setting_endpoint(
    request: Request,
    key: str,
    payload: UpdateSettingRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RuntimeSettingItem:
    """Update a single runtime setting by key."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_runtime_settings(rt)

    entry = RUNTIME_SETTINGS_REGISTRY.get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    if key in ROUTEWISE_SETTINGS_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")

    expected_type = entry["type"]
    value = payload.value
    if expected_type == "bool" and not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"Setting '{key}' expects a boolean value")
    # ``bool`` is a subclass of ``int`` in Python; without the explicit check, a
    # JSON ``true`` would be accepted as an int/float and persisted as ``"True"``,
    # which then fails coercion on read (``int("True")`` raises).
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
    old_value: Any = None
    if old_row is not None:
        raw = old_row.get("value")
        if expected_type == "bool":
            old_value = raw.lower() in ("true", "1", "yes") if isinstance(raw, str) else raw
        elif expected_type == "int":
            old_value = int(raw) if raw is not None else None
        elif expected_type == "float":
            old_value = float(raw) if raw is not None else None
        else:
            old_value = raw
    else:
        from serving.config.settings import get_settings

        old_value = getattr(get_settings(), key, entry["default"])

    await op_store.set_setting(key, str(value), expected_type, admin_id)

    # Invalidate the singleton's TTL cache so the new value is visible immediately.
    rt.invalidate_key(key)

    ip = get_client_ip(request)
    await log_admin_action(
        op_store,
        ip,
        "settings.update",
        None,
        {"key": key, "old_value": old_value, "new_value": value},
    )

    return RuntimeSettingItem(
        key=key,
        value=value,
        value_type=expected_type,
        default_value=entry["default"],
        description=entry["description"],
        min=entry.get("min"),
        max=entry.get("max"),
    )
