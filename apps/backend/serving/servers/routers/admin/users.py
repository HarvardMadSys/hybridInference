"""Admin user management and audit-log endpoints."""

from __future__ import annotations

import json as _json
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from serving.model_access import (
    DISABLED_MODELS_PREFERENCE_KEY,
    get_disabled_models_from_preferences,
    normalize_disabled_models,
)
from serving.schemas_admin import (
    ApproveUserRequest,
    ApproveUserResponse,
    AuditLogEntry,
    BulkUserCostHistoryResponse,
    DeleteUserRequest,
    DeleteUserResponse,
    HardDeleteUserRequest,
    HardDeleteUserResponse,
    ListAuditLogResponse,
    ListUsersResponse,
    RejectUserRequest,
    RejectUserResponse,
    ResumeUserRequest,
    ResumeUserResponse,
    StatusCounts,
    SummaryCard,
    SummaryUserItem,
    UpdateUserRequest,
    UpdateUserResponse,
    UserCostHistoryPoint,
    UserCostHistoryResponse,
    UserDetailResponse,
    UserListItem,
    UsersSummaryResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_log_store,
    get_operational_store,
    get_router,
    verify_admin_access,
)
from serving.servers.routers.admin._common import _serialize_for_audit
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")


@router.get("/users", response_model=ListUsersResponse)
async def list_users(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    sort_by: Literal[
        "created", "cost_today", "cost_month", "cost_alltime", "last_login"
    ] = "created",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    # Phase 1 admin Users redesign — new filters
    min_cost_today: Decimal | None = Query(None, ge=0),
    min_cost_month: Decimal | None = Query(None, ge=0),
    quota_state: Literal["near", "over", "custom", "default"] | None = None,
    provider: str | None = None,
    active_within_hours: int | None = Query(None, ge=1),
    anomaly: bool | None = None,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListUsersResponse:
    """List registered users with optional status filter, search, and sort.

    Query Parameters:
    - status: Filter by status (pending_approval|active|suspended|rejected|deleted)
    - search: Search by email, user_name, user id prefix, or active key prefix
    - sort_by: Sort order (created|cost_today|cost_month|cost_alltime|last_login)
    - limit: Max results (default: 100)
    - offset: Pagination offset
    - min_cost_today / min_cost_month: filter to users whose today/month spend
      meets the threshold (USD).
    - quota_state: ``default`` / ``custom`` filter on whether the user's active
      key has a quota override; ``near`` / ``over`` use today's spend vs quota.
    - provider: keep only users who hit ``provider`` in api_logs in the last
      30 days.
    - active_within_hours: ``last_login_at`` must be within the window.
    - anomaly: when ``true``, keep only users whose today's spend is
      anomalously high vs. their prior 7-day average (today >= $1, history
      >= 3 days, today >= 5x avg).

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    total, rows, sc = await op_store.list_users(
        status=status,
        search=search,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
        min_cost_today=min_cost_today,
        min_cost_month=min_cost_month,
        quota_state=quota_state,
        provider=provider,
        active_within_hours=active_within_hours,
        anomaly=anomaly,
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
                usage_today_usd=Decimal(str(row.get("usage_today", 0))),
                usage_month_usd=Decimal(str(row.get("usage_month", 0))),
                usage_alltime_usd=Decimal(str(row.get("usage_alltime", 0))),
            )
        )

    return ListUsersResponse(total=total, users=users, status_counts=status_counts)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: Cost history + summary endpoints
#
# IMPORTANT: these static-path routes must be registered BEFORE any
# parameterized route whose path could shadow them (e.g. PATCH /users/{id}).
# FastAPI/Starlette matches the first route whose path pattern matches; if a
# parameterized route is registered first, /users/summary matches it and
# Starlette returns 405 because the method doesn't match.
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/users/cost-history", response_model=BulkUserCostHistoryResponse)
async def admin_get_bulk_user_cost_history(
    user_ids: str = "",  # comma-separated
    days: int = 7,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> BulkUserCostHistoryResponse:
    """Bulk daily cost history for many users (one round-trip per page).

    Query params:
    - ``user_ids``: comma-separated user IDs (max 200)
    - ``days``: 1..90 inclusive (default 7)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    if days < 1 or days > 90:
        raise HTTPException(422, "days must be between 1 and 90")

    ids = [s.strip() for s in user_ids.split(",") if s.strip()]
    if not ids:
        return BulkUserCostHistoryResponse(days=days, histories={})
    if len(ids) > 200:
        raise HTTPException(422, "Maximum 200 user_ids per request")

    raw = await op_store.get_bulk_user_cost_history(ids, days=days)
    histories = {
        uid: [
            UserCostHistoryPoint(
                day=p["day"],
                cost_usd=Decimal(str(p["cost_usd"])),
                requests=p["requests"],
            )
            for p in points
        ]
        for uid, points in raw.items()
    }
    return BulkUserCostHistoryResponse(days=days, histories=histories)


@router.get("/users/summary", response_model=UsersSummaryResponse)
async def admin_get_users_summary(
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> UsersSummaryResponse:
    """Aggregated summary stats for the 4 dashboard cards."""
    if not op_store:
        raise HTTPException(500, "Database not configured")

    raw = await op_store.get_users_summary()

    def _card(card_raw: dict) -> SummaryCard:
        return SummaryCard(
            count=card_raw["count"],
            top=[
                SummaryUserItem(
                    id=u["id"],
                    email=u["email"],
                    user_name=u.get("user_name"),
                    role=u.get("role", "free"),
                    today_cost_usd=Decimal(str(u.get("today_cost_usd", 0))),
                    avg_prior_7d_usd=Decimal(str(u.get("avg_prior_7d_usd", 0))),
                    quota_daily_usd=u.get("quota_daily_usd"),
                    multiplier=u.get("multiplier"),
                )
                for u in card_raw["top"]
            ],
        )

    return UsersSummaryResponse(
        pending=_card(raw["pending"]),
        top_spenders_today=_card(raw["top_spenders_today"]),
        anomalies=_card(raw["anomalies"]),
        near_quota=_card(raw["near_quota"]),
    )


@router.get("/users/{user_id}/cost-history", response_model=UserCostHistoryResponse)
async def admin_get_user_cost_history(
    user_id: str,
    days: int = 7,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> UserCostHistoryResponse:
    """Daily cost history for a single user (1..90 days, default 7)."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    if days < 1 or days > 90:
        raise HTTPException(422, "days must be between 1 and 90")

    raw = await op_store.get_user_cost_history(user_id, days=days)
    return UserCostHistoryResponse(
        user_id=user_id,
        days=days,
        points=[
            UserCostHistoryPoint(
                day=p["day"],
                cost_usd=Decimal(str(p["cost_usd"])),
                requests=p["requests"],
            )
            for p in raw
        ],
    )


@router.post("/users/{user_id}/approve", response_model=ApproveUserResponse)
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


@router.post("/users/{user_id}/reject", response_model=RejectUserResponse)
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


@router.get("/users/{user_id}/detail", response_model=UserDetailResponse)
async def get_user_detail(
    user_id: str,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
    router_exec=Depends(get_router),
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
        disabled_models=get_disabled_models_from_preferences(user_row.get("preferences")),
        last_request_at=last_request_at,
    )


@router.patch("/users/{user_id}", response_model=UpdateUserResponse)
async def update_user(
    request: Request,
    user_id: str,
    payload: UpdateUserRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    router_exec=Depends(get_router),
) -> UpdateUserResponse:
    """Update user account status or API key settings (quota).

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
        if k in ("quota_daily_cost_usd", "quota_monthly_cost_usd")
    }
    if key_fields:
        has_key = await op_store.get_active_key_by_account(user_id)
        if not has_key:
            raise HTTPException(409, "User has no active API key to update")

        await op_store.update_key(user_id, **key_fields)
        updated.extend(key_fields)

    if "disabled_models" in payload_dict:
        known_model_ids = {
            adapter.config.id
            for route in router_exec.routes.values()
            for adapter, _weight in route.adapters
            if getattr(adapter, "config", None) is not None
        }
        normalized_disabled_models = normalize_disabled_models(payload_dict["disabled_models"])
        unknown_model_ids = sorted(set(normalized_disabled_models) - known_model_ids)
        if unknown_model_ids:
            raise HTTPException(400, f"Unknown model id(s): {', '.join(unknown_model_ids)}")
        preferences = await op_store.get_user_preferences(user_id)
        preferences = dict(preferences) if isinstance(preferences, dict) else {}
        preferences[DISABLED_MODELS_PREFERENCE_KEY] = normalized_disabled_models
        await op_store.update_user_preferences(user_id, preferences)
        payload_dict["disabled_models"] = normalized_disabled_models
        updated.append("disabled_models")

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


@router.get("/audit-log", response_model=ListAuditLogResponse)
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


@router.post("/users/{user_id}/delete", response_model=DeleteUserResponse)
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
        admin_ip=get_client_ip(request),
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


@router.post("/users/{user_id}/resume", response_model=ResumeUserResponse)
async def resume_user(
    request: Request,
    user_id: str,
    payload: ResumeUserRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ResumeUserResponse:
    """Resume a soft-deleted user — flip status='deleted' → 'active'.

    API keys remain ``revoked`` — the user must re-create one through the
    normal flow.  Only soft-deleted users may be resumed.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    user_row = await op_store.get_user_by_id(user_id)
    if not user_row:
        raise HTTPException(404, f"User '{user_id}' not found")

    if user_row["status"] != "deleted":
        raise HTTPException(
            409,
            f"Cannot resume user with status '{user_row['status']}'. "
            "Only soft-deleted users can be resumed.",
        )

    await op_store.resume_user(
        user_id,
        admin_ip=get_client_ip(request),
        admin_id=admin_id,
        reason=payload.reason,
        email=user_row["email"],
    )

    return ResumeUserResponse(
        user_id=user_id,
        email=user_row["email"],
        status="active",
        message=f"User {user_row['email']} has been resumed.",
    )


@router.post("/users/{user_id}/hard-delete", response_model=HardDeleteUserResponse)
async def hard_delete_user(
    request: Request,
    user_id: str,
    payload: HardDeleteUserRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> HardDeleteUserResponse:
    """Permanently delete a user and all linked rows.

    Two-step gate: only allowed when the user is already soft-deleted
    (status='deleted').  Wipes the user row, all api_keys, sessions/tokens,
    user_daily_cost, prior admin_audit_log entries, and (via the LogStore)
    api_logs and email_broadcast_recipients.  A fresh ``hard_delete_user``
    audit row is recorded.

    The operational store and log store live in different pools (and may be
    different engines), so a true single-transaction guarantee across both
    is not possible.  We purge LogStore-owned rows FIRST, then run the
    operational-store wipe.

    Failure modes:
    - LogStore wipe fails: operational state and audit log are untouched;
      the user remains soft-deleted (``status='deleted'``) and the admin can
      retry the hard-delete.
    - LogStore wipe succeeds but op_store wipe fails: log rows are gone but
      the user row + prior audit entries remain — the user is still
      soft-deleted, so the admin can retry hard-delete (which will re-attempt
      and succeed since the user is still in ``status='deleted'``).

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    if not payload.confirm:
        raise HTTPException(400, "confirm=True is required to hard-delete a user")

    user_row = await op_store.get_user_by_id(user_id)
    if not user_row:
        raise HTTPException(404, f"User '{user_id}' not found")

    if user_row["status"] != "deleted":
        raise HTTPException(
            409,
            "Hard delete only allowed on soft-deleted users. Soft delete first.",
        )

    email = user_row["email"]

    # Purge LogStore-owned rows FIRST (api_logs + email_broadcast_recipients).
    # Lives in a separate pool, so this cannot share the operational-store
    # transaction.  Running this first means if it fails, the user row +
    # audit are untouched and the admin can retry.
    if log_store is not None:
        await log_store.hard_delete_user_data(user_id)

    # Wipe operational rows + write the new hard-delete audit row, atomically.
    await op_store.hard_delete_user(
        user_id,
        admin_ip=get_client_ip(request),
        admin_id=admin_id,
        reason=payload.reason,
        email=email,
    )

    return HardDeleteUserResponse(
        user_id=user_id,
        email=email,
        message=f"User {email} has been permanently deleted.",
    )
