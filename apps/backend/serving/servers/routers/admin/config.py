"""Admin endpoints for the database-backed application configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serving.config import app_config
from serving.schemas_config import ConfigResponse, UpdateConfigRequest
from serving.servers import restart
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access

router = APIRouter(prefix="/admin")


def _response() -> ConfigResponse:
    return ConfigResponse(**app_config.describe(), restart_supported=restart.restart_supported())


@router.get("/config", response_model=ConfigResponse)
async def get_config(_admin_id: str = Depends(verify_admin_access)) -> ConfigResponse:
    """List every setting with its effective value; secrets carry none."""
    return _response()


@router.patch("/config", response_model=ConfigResponse)
async def update_config(
    payload: UpdateConfigRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ConfigResponse:
    """Validate a batch of values together, store it in one transaction, and apply it."""
    try:
        changes = await app_config.update(payload.values, payload.secrets, updated_by=admin_id)
    except app_config.ConfigUpdateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    for change in changes:
        await log_admin_action(op_store, admin_id, "config.update", None, change.audit_details())
    return _response()


@router.delete("/config/{key}", response_model=ConfigResponse)
async def reset_config(
    key: str,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ConfigResponse:
    """Remove a stored value, so the setting falls back to the environment or its default."""
    try:
        change = await app_config.delete(key, updated_by=admin_id)
    except app_config.ConfigUpdateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    await log_admin_action(op_store, admin_id, "config.reset", None, change.audit_details())
    return _response()
