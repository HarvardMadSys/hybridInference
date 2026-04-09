"""Admin API endpoints for user and system management."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.schemas_admin import (
    APIKeyDetailResponse,
    APIKeyDetailUsage,
    APIKeyListItem,
    ApproveUserRequest,
    ApproveUserResponse,
    AuditLogEntry,
    CreateAPIKeyRequest,
    CreateAPIKeyResponse,
    DeleteUserRequest,
    DeleteUserResponse,
    ListAPIKeysResponse,
    ListAuditLogResponse,
    ListUsersResponse,
    RegenerateAPIKeyResponse,
    RejectUserRequest,
    RejectUserResponse,
    RevokeAPIKeyResponse,
    StatusCounts,
    UpdateAPIKeyRequest,
    UpdateAPIKeyResponse,
    UpdateUserRequest,
    UpdateUserResponse,
    UserDetailResponse,
    UserListItem,
)
from serving.servers.auth import (
    generate_api_key,
    hash_api_key,
    log_admin_action,
)
from serving.servers.deps import (
    get_log_store,
    get_operational_store,
    get_rate_limiter,
    get_router,
    get_services,
    verify_admin_access,
)

router = APIRouter()


def _to_json_safe(value: Any) -> Any:
    """Convert values to JSON-serializable primitives."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _to_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_json_safe(item) for item in value]
    return value


def _serialize_for_audit(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize data dict to JSON-safe primitives for audit logging."""
    return {key: _to_json_safe(value) for key, value in data.items()}


@router.get("/stats")
@router.get("/admin/stats")
async def get_stats(
    model: str | None = None,
    provider: str | None = None,
    hours: int = 24,
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Return usage statistics from the log store."""
    if not log_store:
        return {"error": "Database logging not configured"}

    stats = await log_store.get_stats(model_id=model, provider=provider, hours=hours)
    return {
        "period_hours": hours,
        "filters": {"model": model, "provider": provider},
        "stats": stats,
    }


@router.get("/rate-limits/{model_id}")
@router.get("/admin/rate-limits/{model_id}")
async def get_rate_limit_status(model_id: str, rate_limiter=Depends(get_rate_limiter)):
    """Get rate limit status and metrics for a specific model."""
    if not rate_limiter:
        return {"error": "Rate limiting not configured"}
    status = rate_limiter.get_status(model_id)
    if not status.get("configured"):
        raise HTTPException(404, f"No rate limit configured for model '{model_id}'")
    return status


@router.get("/rate-limits")
@router.get("/admin/rate-limits")
async def get_all_rate_limits(rate_limiter=Depends(get_rate_limiter)):
    """Get rate limit metrics for all models."""
    if not rate_limiter:
        return {"error": "Rate limiting not configured"}
    return rate_limiter.get_metrics()


@router.post("/rate-limits/{model_id}/reset")
@router.post("/admin/rate-limits/{model_id}/reset")
async def reset_circuit_breaker(model_id: str, rate_limiter=Depends(get_rate_limiter)):
    """Reset circuit breaker for a model (admin endpoint)."""
    if not rate_limiter:
        return {"error": "Rate limiting not configured"}
    rate_limiter.reset_circuit_breaker(model_id)
    return {"message": f"Circuit breaker reset for {model_id}"}


@router.get("/admin/routing")
async def admin_get_routing(
    router_exec=Depends(get_router), services=Depends(get_services)
) -> dict[str, Any]:
    """Admin alias for routing information."""
    routing_info = {}
    for model_id, route in router_exec.routes.items():
        routing_info[model_id] = [
            {
                "provider": adapter.config.provider,
                "base_url": adapter.config.base_url,
                "weight": f"{weight * 100:.0f}%",
            }
            for adapter, weight in route.adapters
        ]

    response: dict[str, Any] = {
        "routes": routing_info,
        "description": "Weight distribution for each model.",
    }
    if services.routing_manager:
        response["manager_status"] = services.routing_manager.get_status()
    return response


# ========================================
# API Key Management Endpoints
# ========================================


@router.post("/admin/api-keys", response_model=CreateAPIKeyResponse, status_code=201)
async def create_api_key(
    request: Request,
    payload: CreateAPIKeyRequest,
    admin_ip: str = Depends(verify_admin_access),
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
            admin_ip,
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
        tier=payload.tier,
        quota_daily_cost_usd=payload.quota_daily_cost_usd,
        quota_monthly_cost_usd=payload.quota_monthly_cost_usd,
        expires_at=payload.expires_at,
        notes=payload.notes,
        metadata=payload.metadata,
    )

    await log_admin_action(
        op_store,
        admin_ip,
        "create_key",
        payload.user_id,
        {
            "tier": payload.tier,
            "quota_daily_usd": float(payload.quota_daily_cost_usd),
            "key_prefix": key_prefix,
        },
    )

    return CreateAPIKeyResponse(
        api_key=plaintext_key,
        user_id=payload.user_id,
        key_prefix=key_prefix,
        tier=payload.tier,
        quota_daily_cost_usd=payload.quota_daily_cost_usd,
        quota_monthly_cost_usd=payload.quota_monthly_cost_usd,
        expires_at=payload.expires_at,
        created_at=row["created_at"],
    )


@router.get("/admin/api-keys", response_model=ListAPIKeysResponse)
async def list_api_keys(
    request: Request,
    status: str | None = None,
    tier: str | None = None,
    limit: int = 100,
    offset: int = 0,
    admin_ip: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> ListAPIKeysResponse:
    """List all API keys with optional filtering.

    Query Parameters:
    - status: Filter by status (active|suspended|revoked)
    - tier: Filter by tier (free|pro|enterprise)
    - limit: Max results (default: 100)
    - offset: Pagination offset

    Requires: Authorization: Bearer {ADMIN_TOKEN}
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    total, rows = await op_store.list_keys(status=status, tier=tier, limit=limit, offset=offset)

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
                tier=row["tier"],
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


@router.get("/admin/api-keys/{user_id}", response_model=APIKeyDetailResponse)
async def get_api_key_detail(
    request: Request,
    user_id: str,
    admin_ip: str = Depends(verify_admin_access),
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
        cost_today = key_usage.get("cost_today", 0.0)
        cost_month = key_usage.get("cost_month", 0.0)
        requests_today = key_usage.get("requests_today", 0)
        requests_month = key_usage.get("requests_month", 0)
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
        tier=row["tier"],
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


@router.patch("/admin/api-keys/{user_id}", response_model=UpdateAPIKeyResponse)
async def update_api_key(
    request: Request,
    user_id: str,
    payload: UpdateAPIKeyRequest,
    admin_ip: str = Depends(verify_admin_access),
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
        admin_ip,
        "update_key",
        user_id,
        _serialize_for_audit({"updated_fields": list(payload_dict), "new_values": payload_dict}),
    )

    return UpdateAPIKeyResponse(
        user_id=user_id,
        updated_fields=list(payload_dict),
        new_values=payload_dict,
    )


@router.delete("/admin/api-keys/{user_id}", response_model=RevokeAPIKeyResponse)
async def revoke_api_key(
    request: Request,
    user_id: str,
    hard_delete: bool = False,
    admin_ip: str = Depends(verify_admin_access),
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

    await log_admin_action(op_store, admin_ip, action_type, user_id, {"hard_delete": hard_delete})

    return RevokeAPIKeyResponse(user_id=user_id, action=response_action, message=message)


@router.post("/admin/api-keys/{user_id}/regenerate", response_model=RegenerateAPIKeyResponse)
async def regenerate_api_key(
    request: Request,
    user_id: str,
    admin_ip: str = Depends(verify_admin_access),
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
        admin_ip,
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


# ========================================
# User Registration Management Endpoints
# ========================================


@router.get("/admin/users", response_model=ListUsersResponse)
async def list_users(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    sort_by: Literal[
        "created", "cost_today", "cost_month", "cost_alltime", "last_login"
    ] = "created",
    limit: int = 100,
    offset: int = 0,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListUsersResponse:
    """List registered users with optional status filter, search, and sort.

    Query Parameters:
    - status: Filter by status (pending_approval|active|suspended|rejected|deleted)
    - search: Search by email or user_name (case-insensitive ILIKE)
    - sort_by: Sort order (created|cost_today|cost_month|cost_alltime|last_login)
    - limit: Max results (default: 100)
    - offset: Pagination offset

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    total, rows, sc = await op_store.list_users(
        status=status, search=search, sort_by=sort_by, limit=limit, offset=offset
    )

    status_counts = StatusCounts(
        all=sc.get("all", 0),
        pending_approval=sc.get("pending_approval", 0),
        active=sc.get("active", 0),
        suspended=sc.get("suspended", 0),
        rejected=sc.get("rejected", 0),
        deleted=sc.get("deleted", 0),
    )

    if not rows:
        return ListUsersResponse(total=total, users=[], status_counts=status_counts)

    users = []
    for row in rows:
        users.append(
            UserListItem(
                id=row["id"],
                email=row["email"],
                user_name=row["user_name"],
                role=row["role"] or "free",
                status=row["status"],
                email_verified=row["email_verified"],
                approval_note=row.get("approval_note"),
                reviewed_at=row.get("reviewed_at"),
                reviewed_by=row.get("reviewed_by"),
                created_at=row["created_at"],
                last_login_at=row.get("last_login_at"),
                has_key=row.get("key_prefix") is not None,
                key_prefix=row.get("key_prefix"),
                key_status=row.get("key_status"),
                key_tier=row.get("key_tier"),
                usage_today_usd=Decimal(str(row.get("usage_today", 0))),
                usage_month_usd=Decimal(str(row.get("usage_month", 0))),
                usage_alltime_usd=Decimal(str(row.get("usage_alltime", 0))),
            )
        )

    return ListUsersResponse(total=total, users=users, status_counts=status_counts)


@router.post("/admin/users/{user_id}/approve", response_model=ApproveUserResponse)
async def approve_user(
    request: Request,
    user_id: str,
    payload: ApproveUserRequest | None = None,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ApproveUserResponse:
    """Approve a pending user registration.

    Changes user status from pending_approval to active and notifies the user.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    user_row = await op_store.get_user_by_id(user_id)

    if not user_row:
        raise HTTPException(404, f"User '{user_id}' not found")

    if user_row["status"] != "pending_approval":
        raise HTTPException(
            409,
            f"User is not pending approval (current status: {user_row['status']})",
        )

    note = payload.note if payload else None

    await op_store.approve_user(user_id, admin_id=admin_id, note=note)

    await log_admin_action(
        op_store,
        admin_id,
        "approve_user",
        user_id,
        {"email": user_row["email"], "note": note},
    )

    from serving.utils.email import is_email_enabled, send_approval_email

    if is_email_enabled():
        send_approval_email(user_row["email"])

    return ApproveUserResponse(
        user_id=user_id,
        email=user_row["email"],
        status="active",
        message=f"User {user_row['email']} has been approved.",
    )


@router.post("/admin/users/{user_id}/reject", response_model=RejectUserResponse)
async def reject_user(
    request: Request,
    user_id: str,
    payload: RejectUserRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> RejectUserResponse:
    """Reject a pending user registration.

    Changes user status from pending_approval to rejected and notifies the user
    with the provided reason.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    user_row = await op_store.get_user_by_id(user_id)

    if not user_row:
        raise HTTPException(404, f"User '{user_id}' not found")

    if user_row["status"] != "pending_approval":
        raise HTTPException(
            409,
            f"User is not pending approval (current status: {user_row['status']})",
        )

    await op_store.reject_user(user_id, admin_id=admin_id, reason=payload.reason)

    await log_admin_action(
        op_store,
        admin_id,
        "reject_user",
        user_id,
        {"email": user_row["email"], "reason": payload.reason},
    )

    from serving.utils.email import is_email_enabled, send_rejection_email

    if is_email_enabled():
        send_rejection_email(user_row["email"], payload.reason)

    return RejectUserResponse(
        user_id=user_id,
        email=user_row["email"],
        status="rejected",
        message=f"User {user_row['email']} has been rejected.",
    )


@router.get("/admin/users/{user_id}/detail", response_model=UserDetailResponse)
async def get_user_detail(
    user_id: str,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> UserDetailResponse:
    """Get detailed user info including usage analytics.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    user_row = await op_store.get_user_by_id(user_id)
    if not user_row:
        raise HTTPException(404, f"User '{user_id}' not found")

    key_row = await op_store.get_active_key_by_account(user_id)
    has_key = key_row is not None

    usage_today_usd = 0.0
    usage_today_req = 0
    usage_month_usd = 0.0
    usage_month_req = 0
    models_used: list[str] = []
    last_request_at = None

    if has_key and log_store:
        detail = await log_store.get_user_detail_usage(user_id)
        usage_today_usd = detail.get("usage_today_usd", 0.0)
        usage_today_req = detail.get("usage_today_requests", 0)
        usage_month_usd = detail.get("usage_month_usd", 0.0)
        usage_month_req = detail.get("usage_month_requests", 0)
        models_used = detail.get("models_used", [])
        last_request_at = detail.get("last_request_at")

    return UserDetailResponse(
        id=user_row["id"],
        email=user_row["email"],
        user_name=user_row["user_name"],
        role=user_row["role"] or "free",
        status=user_row["status"],
        email_verified=user_row["email_verified"],
        created_at=user_row["created_at"],
        last_login_at=user_row.get("last_login_at"),
        has_key=has_key,
        key_prefix=key_row["key_prefix"] if key_row else None,
        key_tier=key_row["tier"] if key_row else None,
        quota_daily_usd=float(key_row["quota_daily_cost_usd"])
        if key_row and key_row.get("quota_daily_cost_usd")
        else None,
        quota_monthly_usd=float(key_row["quota_monthly_cost_usd"])
        if key_row and key_row.get("quota_monthly_cost_usd")
        else None,
        usage_today_usd=usage_today_usd,
        usage_today_requests=usage_today_req,
        usage_month_usd=usage_month_usd,
        usage_month_requests=usage_month_req,
        models_used=models_used,
        last_request_at=last_request_at,
    )


@router.patch("/admin/users/{user_id}", response_model=UpdateUserResponse)
async def update_user(
    request: Request,
    user_id: str,
    payload: UpdateUserRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> UpdateUserResponse:
    """Update user account status or API key settings (tier, quota).

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    payload_dict = payload.model_dump(exclude_unset=True)
    if not payload_dict:
        raise HTTPException(422, "No fields to update")

    user_row = await op_store.get_user_by_id(user_id)
    if not user_row:
        raise HTTPException(404, f"User '{user_id}' not found")

    updated: list[str] = []
    current_status = user_row["status"]

    # Update role (user-level field on users table)
    if "role" in payload_dict:
        new_role = payload_dict["role"]
        # Guard: admin cannot demote themselves
        if (
            user_row["email"]
            and user_row["email"].lower() == admin_id.lower()
            and new_role != "admin"
        ):
            raise HTTPException(409, "Cannot demote your own admin role.")
        await op_store.update_user_fields(user_id, role=new_role)
        updated.append("role")

    # Update user-level fields
    if "status" in payload_dict:
        new_status = payload_dict["status"]

        # Enforce valid transitions: only active <-> suspended.
        # pending_approval/rejected must go through approve/reject endpoints.
        valid_transitions = {
            ("active", "suspended"),
            ("suspended", "active"),
        }
        if (current_status, new_status) not in valid_transitions:
            raise HTTPException(
                409,
                f"Cannot transition from '{current_status}' to '{new_status}'. "
                f"Use the approve/reject endpoints for pending users.",
            )

        await op_store.update_user_fields(user_id, status=new_status)
        updated.append("status")

        # Suspend: also revoke active API key to cut API access immediately
        if new_status == "suspended":
            await op_store.revoke_key(user_id, hard_delete=False)

    # Update key-level fields
    key_fields = {
        k: v
        for k, v in payload_dict.items()
        if k in ("tier", "quota_daily_cost_usd", "quota_monthly_cost_usd")
    }
    if key_fields:
        has_key = await op_store.get_active_key_by_account(user_id)
        if not has_key:
            raise HTTPException(409, "User has no active API key to update")

        await op_store.update_key(user_id, **key_fields)
        updated.extend(key_fields)

    await log_admin_action(
        op_store,
        admin_id,
        "update_user",
        user_id,
        _serialize_for_audit({"updated_fields": updated, "values": payload_dict}),
    )

    return UpdateUserResponse(
        user_id=user_id,
        updated_fields=updated,
        message=f"Updated {', '.join(updated)} for user {user_id}.",
    )


# ========================================
# Audit Log Endpoint
# ========================================


@router.get("/admin/audit-log", response_model=ListAuditLogResponse)
async def list_audit_log(
    action: str | None = None,
    target_user_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListAuditLogResponse:
    """List admin audit log entries with optional filtering.

    Query Parameters:
    - action: Filter by action type (e.g. approve_user, reject_user)
    - target_user_id: Filter by affected user
    - limit: Max results (default: 50)
    - offset: Pagination offset

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    total, rows = await op_store.list_audit_log(
        action=action, target_user_id=target_user_id, limit=limit, offset=offset
    )

    import json as _json

    entries = []
    for row in rows:
        details = row["details"]
        # asyncpg may return JSONB as a string — parse if needed
        if isinstance(details, str):
            try:
                details = _json.loads(details)
            except (ValueError, TypeError):
                details = {"raw": details}
        entries.append(
            AuditLogEntry(
                id=row["id"],
                timestamp=row["timestamp"],
                admin_ip=row["admin_ip"],
                action=row["action"],
                target_user_id=row["target_user_id"],
                details=details,
                success=row["success"],
            )
        )

    return ListAuditLogResponse(total=total, entries=entries)


# ========================================
# Delete User Endpoint
# ========================================


@router.post("/admin/users/{user_id}/delete", response_model=DeleteUserResponse)
async def delete_user(
    request: Request,
    user_id: str,
    payload: DeleteUserRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> DeleteUserResponse:
    """Soft-delete a user account.

    Sets user status to 'deleted', revokes all API keys, purges sessions and
    tokens. Preserves api_logs and admin_audit_log for compliance.

    Only active or suspended users can be deleted.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    user_row = await op_store.get_user_by_id(user_id)

    if not user_row:
        raise HTTPException(404, f"User '{user_id}' not found")

    if user_row["status"] not in ("active", "suspended"):
        raise HTTPException(
            409,
            f"Cannot delete user with status '{user_row['status']}'. "
            "Only active or suspended users can be deleted.",
        )

    # Atomic: sets status='deleted', revokes keys, purges sessions/tokens,
    # and inserts audit log — all in a single transaction.
    await op_store.delete_user(
        user_id,
        admin_ip=admin_id,
        admin_id=admin_id,
        reason=payload.reason,
        email=user_row["email"],
    )

    return DeleteUserResponse(
        user_id=user_id,
        email=user_row["email"],
        status="deleted",
        message=f"User {user_row['email']} has been deleted.",
    )
