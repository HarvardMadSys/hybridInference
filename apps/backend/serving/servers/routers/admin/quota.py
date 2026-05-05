"""Admin endpoints for per-role daily quota bulk-apply."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    get_runtime_settings,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

logger = get_logger(__name__)
router = APIRouter(prefix="/admin")

Role = Literal["free", "pro", "internal", "admin"]


class RoleQuotaApplyRequest(BaseModel):
    role: Role


class RoleQuotaPreview(BaseModel):
    role: Role
    quota: Decimal
    keys_affected: int
    users_affected: int


class RoleQuotaApplyResult(BaseModel):
    role: Role
    quota: Decimal
    keys_updated: int


def _require_rt(rt: RuntimeSettings | None) -> RuntimeSettings:
    if rt is None:
        raise HTTPException(status_code=503, detail="Runtime settings not initialized")
    return rt


async def _quota_for_role(rt: RuntimeSettings, role: str) -> Decimal:
    key = f"user_daily_quota_{role}"
    if key not in RUNTIME_SETTINGS_REGISTRY:
        raise HTTPException(status_code=400, detail=f"No quota setting for role: {role}")
    val = await rt.get_float(key)
    return Decimal(str(val))


@router.get("/quota/role-apply-preview", response_model=RoleQuotaPreview)
async def preview_role_apply(
    role: Role = Query(...),
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RoleQuotaPreview:
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_rt(rt)
    quota = await _quota_for_role(rt, role)
    keys, users = await op_store.count_active_keys_for_role(role)
    return RoleQuotaPreview(role=role, quota=quota, keys_affected=keys, users_affected=users)


@router.post("/quota/role-apply", response_model=RoleQuotaApplyResult)
async def apply_role_quota(
    request: Request,
    payload: RoleQuotaApplyRequest,
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    rt: RuntimeSettings | None = Depends(get_runtime_settings),
) -> RoleQuotaApplyResult:
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rt = _require_rt(rt)
    quota = await _quota_for_role(rt, payload.role)
    updated = await op_store.apply_role_quota(payload.role, quota)
    try:
        await log_admin_action(
            op_store,
            get_client_ip(request),
            "quota.role_apply",
            None,
            {"role": payload.role, "quota": float(quota), "keys_updated": updated},
        )
    except Exception:
        logger.warning(
            "audit log failed for quota.role_apply role=%s keys_updated=%d",
            payload.role,
            updated,
            exc_info=True,
        )
    return RoleQuotaApplyResult(role=payload.role, quota=quota, keys_updated=updated)
