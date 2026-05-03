"""Admin API key management endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.schemas_admin import (
    APIKeyDetailResponse,
    APIKeyDetailUsage,
    APIKeyListItem,
    CreateAPIKeyRequest,
    CreateAPIKeyResponse,
    ListAPIKeysResponse,
    RegenerateAPIKeyResponse,
    RevokeAPIKeyResponse,
    UpdateAPIKeyRequest,
    UpdateAPIKeyResponse,
)
from serving.servers.auth import (
    generate_api_key,
    hash_api_key,
    log_admin_action,
)
from serving.servers.deps import (
    get_log_store,
    get_operational_store,
    verify_admin_access,
)
from serving.servers.routers.admin._common import _serialize_for_audit

router = APIRouter(prefix="/admin")


@router.post("/api-keys", response_model=CreateAPIKeyResponse, status_code=201)
async def create_api_key(
    request: Request,
    payload: CreateAPIKeyRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> CreateAPIKeyResponse:
    """Create a new API key for a user.

    Returns the plaintext API key ONLY ONCE. Save it immediately.

    Requires: Authorization: Bearer {ADMIN_TOKEN}
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    if await op_store.check_active_key_exists(payload.user_id):
        await log_admin_action(
            op_store,
            admin_id,
            "create_key",
            payload.user_id,
            {"error": "user_id already exists"},
            success=False,
        )
        raise HTTPException(
            status_code=409,
            detail=f"User '{payload.user_id}' already has an active API key. "
            "Revoke it first or use /regenerate endpoint.",
        )

    plaintext_key = generate_api_key()
    key_hash = hash_api_key(plaintext_key)
    key_prefix = plaintext_key[:12]

    row = await op_store.create_key(
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=payload.user_id,
        user_name=payload.user_name,
        quota_daily_cost_usd=payload.quota_daily_cost_usd,
        quota_monthly_cost_usd=payload.quota_monthly_cost_usd,
        expires_at=payload.expires_at,
        notes=payload.notes,
        metadata=payload.metadata,
    )

    await log_admin_action(
        op_store,
        admin_id,
        "create_key",
        payload.user_id,
        {
            "quota_daily_usd": float(payload.quota_daily_cost_usd),
            "key_prefix": key_prefix,
        },
    )

    return CreateAPIKeyResponse(
        api_key=plaintext_key,
        user_id=payload.user_id,
        key_prefix=key_prefix,
        quota_daily_cost_usd=payload.quota_daily_cost_usd,
        quota_monthly_cost_usd=payload.quota_monthly_cost_usd,
        expires_at=payload.expires_at,
        created_at=row["created_at"],
    )


@router.get("/api-keys", response_model=ListAPIKeysResponse)
async def list_api_keys(
    request: Request,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> ListAPIKeysResponse:
    """List all API keys with optional filtering.

    Query Parameters:
    - status: Filter by status (active|suspended|revoked)
    - limit: Max results (default: 100)
    - offset: Pagination offset

    Requires: Authorization: Bearer {ADMIN_TOKEN}
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    total, rows = await op_store.list_keys(status=status, limit=limit, offset=offset)

    if not rows:
        return ListAPIKeysResponse(total=total, keys=[])

    # Batch-fetch usage from operational store counter table
    user_ids = [row["user_id"] for row in rows]
    usage_today_map: dict[str, Any] = {}
    usage_month_map: dict[str, Any] = {}
    if op_store and user_ids:
        usage_today_map = await op_store.get_batch_usage(user_ids, period="today")
        usage_month_map = await op_store.get_batch_usage(user_ids, period="month")

    keys = []
    for row in rows:
        uid = row["user_id"]
        keys.append(
            APIKeyListItem(
                user_id=uid,
                user_name=row["user_name"],
                key_prefix=row["key_prefix"],
                status=row["status"],
                quota_daily_cost_usd=row["quota_daily_cost_usd"],
                quota_monthly_cost_usd=row["quota_monthly_cost_usd"],
                created_at=row["created_at"],
                last_used_at=row["last_used_at"],
                expires_at=row["expires_at"],
                usage_today_usd=Decimal(str(usage_today_map.get(uid, 0))),
                usage_month_usd=Decimal(str(usage_month_map.get(uid, 0))),
                notes=row["notes"],
            )
        )

    return ListAPIKeysResponse(total=total, keys=keys)


@router.get("/api-keys/{user_id}", response_model=APIKeyDetailResponse)
async def get_api_key_detail(
    request: Request,
    user_id: str,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> APIKeyDetailResponse:
    """Get detailed information about a specific API key including usage analytics.

    Requires: Authorization: Bearer {ADMIN_TOKEN}
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    row = await op_store.get_key_detail(user_id)
    if not row:
        raise HTTPException(404, f"User '{user_id}' not found")

    # Fetch usage from log store
    cost_today = 0.0
    cost_month = 0.0
    requests_today = 0
    requests_month = 0
    models_used: list[str] = []
    last_request_at = None

    if log_store:
        key_usage = await log_store.get_key_detail_usage(user_id)
        cost_today = key_usage.get("today", {}).get("cost_usd", 0.0)
        cost_month = key_usage.get("this_month", {}).get("cost_usd", 0.0)
        requests_today = key_usage.get("today", {}).get("requests", 0)
        requests_month = key_usage.get("this_month", {}).get("requests", 0)
        models_used = key_usage.get("models_used", [])
        last_request_at = key_usage.get("last_request_at")

    quota_daily = float(row["quota_daily_cost_usd"]) if row["quota_daily_cost_usd"] else 1000.0
    quota_monthly = float(row["quota_monthly_cost_usd"]) if row["quota_monthly_cost_usd"] else None

    usage = APIKeyDetailUsage(
        today={
            "cost_usd": cost_today,
            "requests": requests_today,
            "quota_remaining_usd": max(0, quota_daily - cost_today),
        },
        this_month={
            "cost_usd": cost_month,
            "requests": requests_month,
            "quota_remaining_usd": (max(0, quota_monthly - cost_month) if quota_monthly else None),
        },
        models_used=models_used,
        last_request_at=last_request_at,
    )

    return APIKeyDetailResponse(
        user_id=row["user_id"],
        user_name=row["user_name"],
        key_prefix=row["key_prefix"],
        status=row["status"],
        quota_daily_cost_usd=row["quota_daily_cost_usd"],
        quota_monthly_cost_usd=row["quota_monthly_cost_usd"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        expires_at=row["expires_at"],
        notes=row["notes"],
        metadata=row["metadata"],
        usage=usage,
    )


@router.patch("/api-keys/{user_id}", response_model=UpdateAPIKeyResponse)
async def update_api_key(
    request: Request,
    user_id: str,
    payload: UpdateAPIKeyRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> UpdateAPIKeyResponse:
    """Update an existing API key's settings.

    All fields are optional - only provided fields will be updated.

    Requires: Authorization: Bearer {ADMIN_TOKEN}
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    payload_dict = payload.model_dump(exclude_unset=True)
    if not payload_dict:
        raise HTTPException(422, "No fields to update")

    existing = await op_store.get_key_detail(user_id)
    if not existing:
        raise HTTPException(404, f"User '{user_id}' not found")

    await op_store.update_key(user_id, **payload_dict)

    await log_admin_action(
        op_store,
        admin_id,
        "update_key",
        user_id,
        _serialize_for_audit({"updated_fields": list(payload_dict), "new_values": payload_dict}),
    )

    return UpdateAPIKeyResponse(
        user_id=user_id,
        updated_fields=list(payload_dict),
        new_values=payload_dict,
    )


@router.delete("/api-keys/{user_id}", response_model=RevokeAPIKeyResponse)
async def revoke_api_key(
    request: Request,
    user_id: str,
    hard_delete: bool = False,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> RevokeAPIKeyResponse:
    """Revoke or delete an API key.

    Query Parameters:
    - hard_delete: If true, permanently delete from database (irreversible)
                   If false (default), set status='revoked' (soft delete)

    Requires: Authorization: Bearer {ADMIN_TOKEN}
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    existing = await op_store.get_key_detail(user_id)
    if not existing:
        raise HTTPException(404, f"User '{user_id}' not found")

    await op_store.revoke_key(user_id, hard_delete=hard_delete)

    if hard_delete:
        action_type = "hard_delete_key"
        response_action = "deleted"
        message = f"API key for user '{user_id}' has been permanently deleted."
    else:
        action_type = "revoke_key"
        response_action = "revoked"
        message = (
            f"API key for user '{user_id}' has been revoked. User can no longer access the API."
        )

    await log_admin_action(op_store, admin_id, action_type, user_id, {"hard_delete": hard_delete})

    return RevokeAPIKeyResponse(user_id=user_id, action=response_action, message=message)


@router.post("/api-keys/{user_id}/regenerate", response_model=RegenerateAPIKeyResponse)
async def regenerate_api_key(
    request: Request,
    user_id: str,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> RegenerateAPIKeyResponse:
    """Regenerate API key for a user (e.g., after suspected compromise).

    This atomically:
    1. Generates a new key
    2. Updates the database
    3. Returns the new plaintext key ONLY ONCE

    The old key is immediately invalidated.

    Requires: Authorization: Bearer {ADMIN_TOKEN}
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    new_plaintext_key = generate_api_key()
    new_key_hash = hash_api_key(new_plaintext_key)
    new_key_prefix = new_plaintext_key[:12]

    try:
        old_key_prefix = await op_store.regenerate_key(
            user_id, new_key_hash=new_key_hash, new_key_prefix=new_key_prefix
        )
    except ValueError:
        raise HTTPException(404, f"User '{user_id}' not found") from None

    await log_admin_action(
        op_store,
        admin_id,
        "regenerate_key",
        user_id,
        {"old_key_prefix": old_key_prefix, "new_key_prefix": new_key_prefix},
    )

    return RegenerateAPIKeyResponse(
        api_key=new_plaintext_key,
        user_id=user_id,
        key_prefix=new_key_prefix,
        old_key_prefix=old_key_prefix,
    )
