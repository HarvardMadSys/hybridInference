"""Admin API endpoints for user and system management."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

import asyncpg

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from serving.admin.provider_quotas import gather_all
from serving.auth.signup_policy import invalidate_allowlist_cache
from serving.schemas_admin import (
    AddSignupAllowedDomainRequest,
    AdminAnalyticsResponse,
    AdminMetricDistribution,
    AdminPerformanceMetricsResponse,
    AdminPerformanceMetricsWindow,
    AdminProviderQuotasResponse,
    AdminRecentRequestItem,
    AdminRecentRequestsResponse,
    AdminRequestMetricsBucket,
    AdminRequestMetricsResponse,
    AdminRequestMetricsWindow,
    AdminTtftScatterModel,
    AdminTtftScatterPoint,
    AdminTtftScatterResponse,
    AnalyticsBreakdownEntry,
    AnalyticsUserEntry,
    ApproveUserRequest,
    ApproveUserResponse,
    AuditLogEntry,
    BroadcastDetailResponse,
    BroadcastListItem,
    BroadcastPreviewRequest,
    BroadcastPreviewResponse,
    BroadcastRecipientItem,
    CreateBroadcastRequest,
    CreateBroadcastResponse,
    DeleteUserRequest,
    DeleteUserResponse,
    HardDeleteUserRequest,
    HardDeleteUserResponse,
    ListAuditLogResponse,
    ListBroadcastsResponse,
    ListSignupAllowedDomainsResponse,
    ListUsersResponse,
    ProviderModelPair,
    ProviderStatsResponse,
    ProviderStatsRow,
    ProviderTokenUsageResponse,
    ProviderTokenUsageRow,
    ProviderTokenUsageTotals,
    ProviderTokenUsageWindow,
    RejectUserRequest,
    RejectUserResponse,
    ResumeUserRequest,
    ResumeUserResponse,
    SignupAllowedDomain,
    SparklineBucket,
    StatusCounts,
    UpdateUserRequest,
    UpdateUserResponse,
    UserDetailResponse,
    UserListItem,
)
from serving.servers.auth import (
    log_admin_action,
)
from serving.servers.deps import (
    get_db_logger,
    get_log_store,
    get_operational_store,
    verify_admin_access,
)
from serving.servers.routers.admin._common import (
    _build_histogram,
    _require_aware_utc,
    _round_or_none,
    _serialize_for_audit,
    _truncate_hour,
)
from serving.utils.email import render_broadcast_template, send_email
from serving.utils.email_scheduler import cancel_broadcast_job, schedule_broadcast
from serving.utils.request_ip import get_client_ip

router = APIRouter()

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


# ========================================
# Resume User Endpoint
# ========================================


@router.post("/admin/users/{user_id}/resume", response_model=ResumeUserResponse)
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


# ========================================
# Hard Delete User Endpoint
# ========================================


@router.post("/admin/users/{user_id}/hard-delete", response_model=HardDeleteUserResponse)
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


# ========================================
# Recent Requests (Admin View)
# ========================================


REQUEST_METRIC_WINDOWS: tuple[tuple[str, str, int, int], ...] = (
    ("5m", "Last 5 min", 5, 1),
    ("1h", "Last 1 hour", 60, 5),
    ("4h", "Last 4 hours", 240, 15),
    ("1d", "Last 1 day", 1440, 60),
    ("1w", "Last 1 week", 10080, 360),
    ("1mo", "Last 1 month", 43200, 1440),
)


@router.get("/admin/request-metrics", response_model=AdminRequestMetricsResponse)
async def admin_get_request_metrics(
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminRequestMetricsResponse:
    """Return request count trends for admin dashboard lookback windows."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    windows: list[AdminRequestMetricsWindow] = []
    async with db_logger.pool.acquire() as conn:
        for key, label, window_minutes, bucket_minutes in REQUEST_METRIC_WINDOWS:
            rows = await conn.fetch(
                """
                WITH config AS (
                    SELECT ($2::int * 60) AS bucket_seconds
                ),
                bounds AS (
                    SELECT
                        date_trunc('minute', NOW()) AS end_time,
                        date_trunc('minute', NOW())
                            - ($1::int * interval '1 minute') AS start_time,
                        to_timestamp(
                            floor(
                                extract(
                                    epoch FROM date_trunc('minute', NOW())
                                        - ($1::int * interval '1 minute')
                                ) / config.bucket_seconds
                            ) * config.bucket_seconds
                        ) AS aligned_start
                    FROM config
                ),
                series AS (
                    SELECT generate_series(
                        (SELECT aligned_start FROM bounds),
                        (SELECT end_time FROM bounds),
                        $2::int * interval '1 minute'
                    ) AS bucket_start
                ),
                bucketed_logs AS (
                    SELECT
                        to_timestamp(
                            floor(extract(epoch from timestamp) / ($2::int * 60))
                            * ($2::int * 60)
                        ) AS bucket_start,
                        COUNT(*) AS request_count,
                        COUNT(*) FILTER (
                            WHERE status_code >= 200 AND status_code < 400
                        ) AS success_count,
                        COUNT(*) FILTER (
                            WHERE error IS NOT NULL
                               OR status_code IS NULL
                               OR status_code < 200
                               OR status_code >= 400
                        ) AS error_count,
                        COUNT(latency_ms) FILTER (WHERE latency_ms IS NOT NULL)
                            AS latency_count,
                        SUM(latency_ms) FILTER (WHERE latency_ms IS NOT NULL)
                            AS latency_sum_ms,
                        AVG(latency_ms) FILTER (WHERE latency_ms IS NOT NULL)
                            AS avg_latency_ms
                    FROM api_logs, bounds
                    WHERE timestamp >= bounds.start_time
                      AND timestamp <= bounds.end_time
                    GROUP BY 1
                )
                SELECT
                    series.bucket_start,
                    COALESCE(bucketed_logs.request_count, 0) AS request_count,
                    COALESCE(bucketed_logs.success_count, 0) AS success_count,
                    COALESCE(bucketed_logs.error_count, 0) AS error_count,
                    COALESCE(bucketed_logs.latency_count, 0) AS latency_count,
                    COALESCE(bucketed_logs.latency_sum_ms, 0) AS latency_sum_ms,
                    bucketed_logs.avg_latency_ms
                FROM series
                LEFT JOIN bucketed_logs
                  ON bucketed_logs.bucket_start = series.bucket_start
                ORDER BY series.bucket_start ASC
                """,
                window_minutes,
                bucket_minutes,
            )

            buckets = [
                AdminRequestMetricsBucket(
                    start_time=row["bucket_start"],
                    request_count=int(row["request_count"] or 0),
                    success_count=int(row["success_count"] or 0),
                    error_count=int(row["error_count"] or 0),
                    avg_latency_ms=(
                        round(float(row["avg_latency_ms"]), 1)
                        if row["avg_latency_ms"] is not None
                        else None
                    ),
                )
                for row in rows
            ]
            total_requests = sum(bucket.request_count for bucket in buckets)
            success_requests = sum(bucket.success_count for bucket in buckets)
            error_requests = sum(bucket.error_count for bucket in buckets)
            latency_count = sum(int(row["latency_count"] or 0) for row in rows)
            latency_sum_ms = sum(float(row["latency_sum_ms"] or 0) for row in rows)
            windows.append(
                AdminRequestMetricsWindow(
                    key=key,
                    label=label,
                    window_minutes=window_minutes,
                    bucket_minutes=bucket_minutes,
                    total_requests=total_requests,
                    success_requests=success_requests,
                    error_requests=error_requests,
                    avg_latency_ms=(
                        round(latency_sum_ms / latency_count, 1) if latency_count else None
                    ),
                    buckets=buckets,
                )
            )

    return AdminRequestMetricsResponse(
        generated_at=datetime.now(timezone.utc),
        windows=windows,
    )


# ========================================
# Performance Metrics (Admin View)
# ========================================


# Inner edges for histogram buckets, passed to width_bucket(x, ARRAY[...]).
# With N = len(edges), width_bucket returns bucket 0 for x < edges[0]
# (underflow; ignored here because these metrics are never negative),
# buckets 1..N-1 for bounded ranges [edges[k-1], edges[k]), and bucket N
# for the final open-ended overflow bucket [edges[-1], +inf).
_TOKEN_HISTOGRAM_EDGES: tuple[float, ...] = (0, 32, 128, 512, 2048, 8192, 32768, 131072)
_LATENCY_HISTOGRAM_EDGES: tuple[float, ...] = (0, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
_THROUGHPUT_HISTOGRAM_EDGES: tuple[float, ...] = (0, 5, 10, 25, 50, 100, 250, 500, 1000)


def _distribution_from_row(
    row: Any,
    prefix: str,
    edges: tuple[float, ...],
    bucket_counts: dict[int, int],
) -> AdminMetricDistribution:
    """Build a distribution from a stats row + histogram counts dict."""
    return AdminMetricDistribution(
        count=int(row[f"{prefix}_count"] or 0),
        mean=_round_or_none(row[f"{prefix}_mean"]),
        min=_round_or_none(row[f"{prefix}_min"]),
        max=_round_or_none(row[f"{prefix}_max"]),
        p50=_round_or_none(row[f"{prefix}_p50"]),
        p90=_round_or_none(row[f"{prefix}_p90"]),
        p95=_round_or_none(row[f"{prefix}_p95"]),
        p99=_round_or_none(row[f"{prefix}_p99"]),
        histogram=_build_histogram(edges, bucket_counts),
    )


# Shared CTE prefix used by both the stats query and the histogram query for
# each window. Defining it once keeps the row filter / throughput derivation in sync
# so the "histogram sums to count" invariant cannot drift.
_PERF_METRICS_CTE = """
WITH base AS (
    SELECT
        prompt_tokens,
        completion_tokens,
        ttft_ms,
        latency_ms,
        stream
    FROM api_logs
    WHERE timestamp >= NOW() - ($1::int * interval '1 minute')
      AND status_code BETWEEN 200 AND 399
),
derived AS (
    SELECT
        prompt_tokens,
        completion_tokens,
        CASE
            WHEN stream = TRUE AND ttft_ms IS NOT NULL
                THEN ttft_ms::float
        END AS ttft_ms,
        CASE
            WHEN stream = TRUE
                AND ttft_ms IS NOT NULL
                AND completion_tokens IS NOT NULL
                AND completion_tokens > 1
                AND latency_ms IS NOT NULL
                AND latency_ms > ttft_ms
                THEN (completion_tokens - 1)::float * 1000.0
                     / NULLIF(latency_ms - ttft_ms, 0)
        END AS throughput_tps
    FROM base
)
"""


@router.get("/admin/performance-metrics", response_model=AdminPerformanceMetricsResponse)
async def admin_get_performance_metrics(
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminPerformanceMetricsResponse:
    """Return prompt/response length and latency distributions per lookback window.

    For each window, computes percentiles (p50/p90/p95/p99), mean/min/max, count,
    and a small histogram for:

    - prompt_tokens (over rows where prompt_tokens > 0)
    - completion_tokens (over rows where completion_tokens > 0)
    - ttft_ms (over streaming rows with ttft_ms NOT NULL)
    - throughput_tps (over streaming rows with completion_tokens > 1, derived from
      (completion_tokens - 1) * 1000 / (latency_ms - ttft_ms); requires
      latency_ms > ttft_ms — non-positive decode time clamped to NULL)

    Only successful requests (status_code 200-399) are included.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    token_edges = list(_TOKEN_HISTOGRAM_EDGES)
    latency_edges = list(_LATENCY_HISTOGRAM_EDGES)
    throughput_edges = list(_THROUGHPUT_HISTOGRAM_EDGES)

    windows: list[AdminPerformanceMetricsWindow] = []
    async with db_logger.pool.acquire() as conn:
        for key, label, window_minutes, _bucket_minutes in REQUEST_METRIC_WINDOWS:
            # Aggregate stats — one row, one query per window.
            stats_row = await conn.fetchrow(
                _PERF_METRICS_CTE
                + """
                SELECT
                    COUNT(*) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_count,
                    AVG(prompt_tokens) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_mean,
                    MIN(prompt_tokens) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_min,
                    MAX(prompt_tokens) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_max,
                    percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY prompt_tokens
                    ) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_p50,
                    percentile_cont(0.9) WITHIN GROUP (
                        ORDER BY prompt_tokens
                    ) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_p90,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY prompt_tokens
                    ) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_p95,
                    percentile_cont(0.99) WITHIN GROUP (
                        ORDER BY prompt_tokens
                    ) FILTER (
                        WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                    ) AS pt_p99,

                    COUNT(*) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_count,
                    AVG(completion_tokens) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_mean,
                    MIN(completion_tokens) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_min,
                    MAX(completion_tokens) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_max,
                    percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY completion_tokens
                    ) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_p50,
                    percentile_cont(0.9) WITHIN GROUP (
                        ORDER BY completion_tokens
                    ) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_p90,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY completion_tokens
                    ) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_p95,
                    percentile_cont(0.99) WITHIN GROUP (
                        ORDER BY completion_tokens
                    ) FILTER (
                        WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                    ) AS ct_p99,

                    COUNT(*) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_count,
                    AVG(ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_mean,
                    MIN(ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_min,
                    MAX(ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL) AS tt_max,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY ttft_ms)
                        FILTER (WHERE ttft_ms IS NOT NULL) AS tt_p50,
                    percentile_cont(0.9) WITHIN GROUP (ORDER BY ttft_ms)
                        FILTER (WHERE ttft_ms IS NOT NULL) AS tt_p90,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY ttft_ms)
                        FILTER (WHERE ttft_ms IS NOT NULL) AS tt_p95,
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY ttft_ms)
                        FILTER (WHERE ttft_ms IS NOT NULL) AS tt_p99,

                    COUNT(*) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_count,
                    AVG(throughput_tps) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_mean,
                    MIN(throughput_tps) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_min,
                    MAX(throughput_tps) FILTER (WHERE throughput_tps IS NOT NULL) AS tp_max,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY throughput_tps)
                        FILTER (WHERE throughput_tps IS NOT NULL) AS tp_p50,
                    percentile_cont(0.9) WITHIN GROUP (ORDER BY throughput_tps)
                        FILTER (WHERE throughput_tps IS NOT NULL) AS tp_p90,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY throughput_tps)
                        FILTER (WHERE throughput_tps IS NOT NULL) AS tp_p95,
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY throughput_tps)
                        FILTER (WHERE throughput_tps IS NOT NULL) AS tp_p99
                FROM derived
                """,
                window_minutes,
            )

            # Histogram counts — one row per (metric, bucket).
            hist_rows = await conn.fetch(
                _PERF_METRICS_CTE
                + """
                SELECT 'prompt_tokens' AS metric,
                       width_bucket(prompt_tokens::float, $2::float[]) AS bucket,
                       COUNT(*) AS cnt
                FROM derived
                WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0
                GROUP BY bucket
                UNION ALL
                SELECT 'completion_tokens' AS metric,
                       width_bucket(completion_tokens::float, $2::float[]) AS bucket,
                       COUNT(*) AS cnt
                FROM derived
                WHERE completion_tokens IS NOT NULL AND completion_tokens > 0
                GROUP BY bucket
                UNION ALL
                SELECT 'ttft_ms' AS metric,
                       width_bucket(ttft_ms, $3::float[]) AS bucket,
                       COUNT(*) AS cnt
                FROM derived
                WHERE ttft_ms IS NOT NULL
                GROUP BY bucket
                UNION ALL
                SELECT 'throughput_tps' AS metric,
                       width_bucket(throughput_tps, $4::float[]) AS bucket,
                       COUNT(*) AS cnt
                FROM derived
                WHERE throughput_tps IS NOT NULL
                GROUP BY bucket
                """,
                window_minutes,
                token_edges,
                latency_edges,
                throughput_edges,
            )

            histograms: dict[str, dict[int, int]] = {
                "prompt_tokens": {},
                "completion_tokens": {},
                "ttft_ms": {},
                "throughput_tps": {},
            }
            for row in hist_rows:
                metric = row["metric"]
                bucket = int(row["bucket"] or 0)
                cnt = int(row["cnt"] or 0)
                # width_bucket can return 0 for negative values; for token
                # metrics this can't happen (filtered to > 0), but for throughput_tps
                # we already clamped. Fold any underflow into bucket 1 just
                # in case so that sum(histogram) == count holds.
                target = bucket if bucket >= 1 else 1
                histograms[metric][target] = histograms[metric].get(target, 0) + cnt

            # `stats_row` is always non-None: an aggregate SELECT without
            # GROUP BY returns exactly one row even when `derived` is empty.
            window = AdminPerformanceMetricsWindow(
                key=key,
                label=label,
                window_minutes=window_minutes,
                prompt_tokens=_distribution_from_row(
                    stats_row, "pt", _TOKEN_HISTOGRAM_EDGES, histograms["prompt_tokens"]
                ),
                completion_tokens=_distribution_from_row(
                    stats_row, "ct", _TOKEN_HISTOGRAM_EDGES, histograms["completion_tokens"]
                ),
                ttft_ms=_distribution_from_row(
                    stats_row, "tt", _LATENCY_HISTOGRAM_EDGES, histograms["ttft_ms"]
                ),
                throughput_tps=_distribution_from_row(
                    stats_row, "tp", _THROUGHPUT_HISTOGRAM_EDGES, histograms["throughput_tps"]
                ),
            )
            windows.append(window)

    return AdminPerformanceMetricsResponse(
        generated_at=datetime.now(timezone.utc),
        windows=windows,
    )


_DECODE_MIN_WINDOW_MS = 2000
_DECODE_MIN_TOKENS = 8


def _decode_throughput_tps(
    stream: bool | None,
    latency_ms: int | None,
    ttft_ms: int | None,
    completion_tokens: int | None,
) -> float | None:
    """Output-token throughput (tok/s) over the decode phase, or None if undefined.

    Matches the convention used by /admin/performance-metrics: first token is
    delivered at ttft_ms, so the decode phase produces (completion_tokens - 1)
    tokens during (latency_ms - ttft_ms). Streaming-only; needs >1 output token.

    Additionally returns None when the decode window is shorter than
    `_DECODE_MIN_WINDOW_MS` (2000 ms) or fewer than `_DECODE_MIN_TOKENS` (8)
    completion tokens were produced. Sub-2-second decode windows and very short
    streams produce noise-dominated throughput numbers (e.g. ~100k tok/s) when
    upstream SSE is buffered or the response collapses to ~0-1 ms of decode
    time, so we render those rows as undefined rather than displaying
    physically implausible values.
    """
    if stream is not True:
        return None
    if ttft_ms is None or ttft_ms <= 0:
        return None
    if latency_ms is None or latency_ms <= ttft_ms:
        return None
    if completion_tokens is None or completion_tokens <= 1:
        return None
    if latency_ms - ttft_ms < _DECODE_MIN_WINDOW_MS:
        return None
    if completion_tokens < _DECODE_MIN_TOKENS:
        return None
    return (completion_tokens - 1) / ((latency_ms - ttft_ms) / 1000.0)


@router.get("/admin/ttft-scatter", response_model=AdminTtftScatterResponse)
async def admin_get_ttft_scatter(
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminTtftScatterResponse:
    """Return TTFT vs input length scatter data per (model, provider).

    For each (model_id, provider) pair, returns up to the last 1000 successful
    streaming requests with a recorded TTFT and a non-empty prompt. The query
    is bounded to the last 30 days so it stays bounded as `api_logs` grows.
    `cache_hit` is true iff `cache_read_tokens > 0`.
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT
                    model_id,
                    provider,
                    prompt_tokens,
                    ttft_ms,
                    cache_read_tokens,
                    timestamp,
                    ROW_NUMBER() OVER (
                        PARTITION BY model_id, provider ORDER BY timestamp DESC
                    ) AS rn
                FROM api_logs
                WHERE timestamp >= NOW() - INTERVAL '30 days'
                  AND ttft_ms IS NOT NULL
                  AND prompt_tokens IS NOT NULL
                  AND prompt_tokens > 0
                  AND status_code BETWEEN 200 AND 399
                  AND stream = TRUE
            )
            SELECT model_id, provider, prompt_tokens, ttft_ms,
                   cache_read_tokens, timestamp
            FROM ranked
            WHERE rn <= 1000
            ORDER BY model_id, provider, timestamp DESC
            """
        )

    by_pair: dict[tuple[str, str], list[AdminTtftScatterPoint]] = {}
    for row in rows:
        key = (row["model_id"], row["provider"])
        cache_read = row["cache_read_tokens"]
        point = AdminTtftScatterPoint(
            prompt_tokens=int(row["prompt_tokens"]),
            ttft_ms=int(row["ttft_ms"]),
            cache_hit=cache_read is not None and cache_read > 0,
            timestamp=row["timestamp"],
        )
        by_pair.setdefault(key, []).append(point)

    models = [
        AdminTtftScatterModel(model_id=model_id, provider=provider, points=points)
        for (model_id, provider), points in by_pair.items()
    ]
    models.sort(key=lambda m: len(m.points), reverse=True)

    return AdminTtftScatterResponse(models=models)


@router.get("/admin/recent-requests", response_model=AdminRecentRequestsResponse)
async def admin_list_recent_requests(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    user_id: str | None = None,
    model_id: str | None = None,
    status_code: int | None = None,
    errors_only: bool = False,
    admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminRecentRequestsResponse:
    """List recent API requests across all users.

    Query Parameters:
    - limit: Max results (default: 50, max: 200)
    - offset: Pagination offset
    - user_id: Filter by user ID
    - model_id: Filter by model ID
    - status_code: Filter by HTTP status code
    - errors_only: If true, only show requests with errors

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    # Build WHERE clause
    where_clauses: list[str] = []
    params: list[Any] = []

    if user_id:
        where_clauses.append(f"l.user_id = ${len(params) + 1}")
        params.append(user_id)

    if model_id:
        where_clauses.append(f"l.model_id = ${len(params) + 1}")
        params.append(model_id)

    if status_code is not None:
        where_clauses.append(f"l.status_code = ${len(params) + 1}")
        params.append(status_code)

    if errors_only:
        where_clauses.append(
            "(l.error IS NOT NULL OR l.status_code IS NULL "
            "OR l.status_code < 200 OR l.status_code >= 400)"
        )

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    async with db_logger.pool.acquire() as conn:
        # Get total count
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) as total FROM api_logs l {where_sql}",
            *params,
        )
        total = int(count_row["total"] or 0) if count_row else 0

        # Get paginated results
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2
        rows = await conn.fetch(
            f"""
            SELECT
                l.request_id, l.user_id, u.user_name, u.email AS user_email,
                l.model_id, l.provider, l.timestamp,
                l.status_code, l.latency_ms, l.ttft_ms, l.stream,
                l.prompt_tokens, l.completion_tokens, l.reasoning_tokens,
                l.cache_read_tokens, l.cache_write_tokens,
                l.total_tokens, l.cost_usd, l.prompt, l.response, l.error,
                l.metadata->>'ip' AS user_ip
            FROM api_logs l
            LEFT JOIN users u ON u.id = l.user_id
            {where_sql}
            ORDER BY l.timestamp DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *params,
            limit,
            offset,
        )

    requests = [
        AdminRecentRequestItem(
            request_id=row["request_id"],
            user_id=row["user_id"],
            user_name=row["user_name"],
            user_email=row["user_email"],
            user_ip=row["user_ip"],
            model_id=row["model_id"],
            provider=row["provider"],
            timestamp=row["timestamp"],
            status_code=row["status_code"],
            latency_ms=row["latency_ms"],
            ttft_ms=row["ttft_ms"],
            decode_throughput_tps=_decode_throughput_tps(
                row["stream"],
                row["latency_ms"],
                row["ttft_ms"],
                row["completion_tokens"],
            ),
            stream=row["stream"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
            total_tokens=row["total_tokens"],
            cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
            prompt=row["prompt"],
            response=row["response"],
            error=row["error"],
        )
        for row in rows
    ]

    return AdminRecentRequestsResponse(requests=requests, total=total, limit=limit, offset=offset)


# Period → (lookback_minutes, bucket_minutes)
_ANALYTICS_PERIODS: dict[str, tuple[int, int]] = {
    "hour": (60, 5),
    "day": (1440, 60),
    "week": (10080, 1440),
    "month": (43200, 1440),
}


@router.get("/admin/analytics", response_model=AdminAnalyticsResponse)
async def admin_get_analytics(
    period: Literal["hour", "day", "week", "month"] = "day",
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> AdminAnalyticsResponse:
    """Return analytics summary for the admin analytics tab."""
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    lookback_minutes, bucket_minutes = _ANALYTICS_PERIODS[period]

    # Acquire ONE connection and run all queries sequentially. Acquiring 5
    # connections via asyncio.gather can starve the pool when two admin
    # analytics requests arrive concurrently (pool max_size is small).
    async with db_logger.pool.acquire() as conn:
        active_users_row = await conn.fetchrow(
            """
            SELECT COUNT(DISTINCT user_id) AS cnt
            FROM api_logs
            WHERE timestamp >= NOW() - ($1 * interval '1 minute')
              AND user_id IS NOT NULL
            """,
            lookback_minutes,
        )
        active_users = int(active_users_row["cnt"] or 0)

        top_users_rows = await conn.fetch(
            """
            WITH totals AS (
                SELECT COUNT(*) AS grand_total
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                  AND user_id IS NOT NULL
            ),
            ranked AS (
                SELECT
                    l.user_id,
                    COALESCE(u.email, l.user_id) AS email,
                    COUNT(*) AS req_count
                FROM api_logs l
                LEFT JOIN users u ON u.id = l.user_id
                WHERE l.timestamp >= NOW() - ($1 * interval '1 minute')
                  AND l.user_id IS NOT NULL
                GROUP BY l.user_id, u.email
                ORDER BY req_count DESC
                LIMIT 10
            )
            SELECT
                r.user_id,
                r.email,
                r.req_count,
                CASE WHEN t.grand_total > 0
                     THEN r.req_count::float / t.grand_total
                     ELSE 0 END AS fraction
            FROM ranked r, totals t
            """,
            lookback_minutes,
        )

        by_model_rows = await conn.fetch(
            """
            WITH totals AS (
                SELECT COUNT(*) AS grand_total
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
            ),
            ranked AS (
                SELECT model_id AS name, COUNT(*) AS req_count
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                GROUP BY model_id
                ORDER BY req_count DESC
                LIMIT 5
            ),
            top_total AS (
                SELECT COALESCE(SUM(req_count), 0) AS top_req_count FROM ranked
            )
            SELECT r.name, r.req_count,
                CASE WHEN t.grand_total > 0 THEN r.req_count::float / t.grand_total ELSE 0 END AS fraction
            FROM ranked r, totals t
            UNION ALL
            SELECT 'others',
                GREATEST(t.grand_total - tt.top_req_count, 0),
                CASE WHEN t.grand_total > 0
                     THEN GREATEST(t.grand_total - tt.top_req_count, 0)::float / t.grand_total
                     ELSE 0 END
            FROM totals t, top_total tt
            WHERE t.grand_total > tt.top_req_count
            ORDER BY req_count DESC
            """,
            lookback_minutes,
        )

        by_provider_rows = await conn.fetch(
            """
            WITH totals AS (
                SELECT COUNT(*) AS grand_total
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
            ),
            ranked AS (
                SELECT provider AS name, COUNT(*) AS req_count
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 * interval '1 minute')
                GROUP BY provider
                ORDER BY req_count DESC
                LIMIT 4
            ),
            top_total AS (
                SELECT COALESCE(SUM(req_count), 0) AS top_req_count FROM ranked
            )
            SELECT r.name, r.req_count,
                CASE WHEN t.grand_total > 0 THEN r.req_count::float / t.grand_total ELSE 0 END AS fraction
            FROM ranked r, totals t
            UNION ALL
            SELECT 'others',
                GREATEST(t.grand_total - tt.top_req_count, 0),
                CASE WHEN t.grand_total > 0
                     THEN GREATEST(t.grand_total - tt.top_req_count, 0)::float / t.grand_total
                     ELSE 0 END
            FROM totals t, top_total tt
            WHERE t.grand_total > tt.top_req_count
            ORDER BY req_count DESC
            """,
            lookback_minutes,
        )

        # Sparkline: align with the date_trunc + generate_series pattern used by
        # /admin/request-metrics so admin chart bucket boundaries are consistent.
        sparkline_rows = await conn.fetch(
            """
            WITH config AS (
                SELECT ($2::int * 60) AS bucket_seconds
            ),
            bounds AS (
                SELECT
                    date_trunc('minute', NOW()) AS end_time,
                    date_trunc('minute', NOW())
                        - ($1::int * interval '1 minute') AS start_time,
                    to_timestamp(
                        floor(
                            extract(
                                epoch FROM date_trunc('minute', NOW())
                                    - ($1::int * interval '1 minute')
                            ) / config.bucket_seconds
                        ) * config.bucket_seconds
                    ) AS aligned_start
                FROM config
            ),
            series AS (
                SELECT generate_series(
                    (SELECT aligned_start FROM bounds),
                    (SELECT end_time FROM bounds),
                    $2::int * interval '1 minute'
                ) AS bucket_start
            ),
            bucketed_logs AS (
                SELECT
                    to_timestamp(
                        floor(extract(epoch from timestamp) / ($2::int * 60))
                        * ($2::int * 60)
                    ) AS bucket_start,
                    COUNT(*) AS request_count
                FROM api_logs, bounds
                WHERE timestamp >= bounds.start_time
                  AND timestamp <= bounds.end_time
                GROUP BY 1
            )
            SELECT
                series.bucket_start,
                COALESCE(bucketed_logs.request_count, 0) AS request_count
            FROM series
            LEFT JOIN bucketed_logs
              ON bucketed_logs.bucket_start = series.bucket_start
            ORDER BY series.bucket_start ASC
            """,
            lookback_minutes,
            bucket_minutes,
        )

    return AdminAnalyticsResponse(
        period=period,
        active_users=active_users,
        sparkline=[
            SparklineBucket(
                start_time=row["bucket_start"],
                request_count=int(row["request_count"] or 0),
            )
            for row in sparkline_rows
        ],
        top_users=[
            AnalyticsUserEntry(
                email=str(row["email"]),
                user_id=str(row["user_id"]),
                requests=int(row["req_count"]),
                fraction=float(row["fraction"]),
            )
            for row in top_users_rows
        ],
        by_model=[
            AnalyticsBreakdownEntry(
                name=str(row["name"]) if row["name"] else "unknown",
                requests=int(row["req_count"]),
                fraction=float(row["fraction"]),
            )
            for row in by_model_rows
        ],
        by_provider=[
            AnalyticsBreakdownEntry(
                name=str(row["name"]) if row["name"] else "unknown",
                requests=int(row["req_count"]),
                fraction=float(row["fraction"]),
            )
            for row in by_provider_rows
        ],
        generated_at=datetime.now(timezone.utc),
    )


# ── Broadcast Email Endpoints ──────────────────────────────────────────────


def _render_or_422(req: BroadcastPreviewRequest) -> dict[str, str]:
    """Render template (or pass through custom content), mapping ValueError to 422."""
    try:
        return render_broadcast_template(
            req.template_key,
            req.template_vars,
            custom_subject=req.subject,
            custom_body_html=req.body_html,
            custom_body_text=req.body_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/admin/broadcast-email/preview", response_model=BroadcastPreviewResponse)
async def preview_broadcast(
    req: BroadcastPreviewRequest,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
):
    """Return recipient count and rendered email preview without sending."""
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    rendered = _render_or_422(req)

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) as cnt FROM users
            WHERE role = ANY($1::text[])
              AND status = ANY($2::text[])
            """,
            req.target_roles or [],
            req.target_statuses or [],
        )
    count = row["cnt"] if row else 0

    return BroadcastPreviewResponse(
        recipient_count=count,
        rendered_subject=rendered["subject"],
        rendered_body_html=rendered["body_html"],
        rendered_body_text=rendered["body_text"],
    )


@router.post("/admin/broadcast-email/test")
async def test_broadcast_email(
    req: BroadcastPreviewRequest,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
):
    """Send a test email to the requesting admin's address only."""
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    rendered = _render_or_422(req)
    if not rendered["subject"] or not rendered["body_html"]:
        raise HTTPException(status_code=422, detail="subject and body_html are required")

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT email FROM users WHERE email = $1", admin)
    if not row:
        raise HTTPException(
            status_code=400, detail="Admin user not found — cannot resolve email address"
        )
    admin_email = row["email"]

    # smtplib is sync; offload to a thread so we don't block the event loop
    # while waiting on the SMTP server.
    import asyncio as _asyncio

    ok = await _asyncio.to_thread(
        send_email,
        admin_email,
        f"[TEST] {rendered['subject']}",
        rendered["body_html"],
        rendered["body_text"],
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to send test email (SMTP error)")
    return {"message": f"Test email sent to {admin_email}"}


@router.post("/admin/broadcast-email", response_model=CreateBroadcastResponse)
async def create_broadcast(
    req: CreateBroadcastRequest,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
):
    """Create a broadcast, snapshot its recipients, and schedule (or fire immediately)."""
    import uuid as _uuid

    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    rendered = _render_or_422(req)
    if not rendered["subject"] or not rendered["body_html"]:
        raise HTTPException(status_code=422, detail="subject and body_html are required")

    broadcast_id = str(_uuid.uuid4())

    async with db.pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
                INSERT INTO email_broadcasts
                    (id, subject, body_html, body_text, template_key, template_vars,
                     target_roles, target_statuses, status, scheduled_at, created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
            broadcast_id,
            rendered["subject"],
            rendered["body_html"],
            rendered["body_text"],
            req.template_key,
            req.template_vars,
            req.target_roles or [],
            req.target_statuses or [],
            "scheduled",
            req.scheduled_at,
            admin,
        )

        # Snapshot recipients at create time so the audience matches what the admin
        # previewed and can't drift between scheduling and execution.
        recipients = await conn.fetch(
            """
                SELECT id, email FROM users
                WHERE role = ANY($1::text[])
                  AND status = ANY($2::text[])
                """,
            req.target_roles or [],
            req.target_statuses or [],
        )
        if recipients:
            await conn.executemany(
                """
                    INSERT INTO email_broadcast_recipients (broadcast_id, user_id, email, status)
                    VALUES ($1, $2, $3, 'pending')
                    ON CONFLICT DO NOTHING
                    """,
                [(broadcast_id, r["id"], r["email"]) for r in recipients],
            )
        await conn.execute(
            "UPDATE email_broadcasts SET recipient_count = $1 WHERE id = $2",
            len(recipients),
            broadcast_id,
        )

    # If APScheduler can't accept the job we don't want a row that says
    # "scheduled" forever — mark it failed and surface a 503.
    try:
        schedule_broadcast(broadcast_id, req.scheduled_at)
    except Exception as exc:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE email_broadcasts SET status = 'failed' WHERE id = $1",
                broadcast_id,
            )
        raise HTTPException(
            status_code=503,
            detail=f"Failed to register broadcast with scheduler: {exc}",
        ) from exc

    await log_admin_action(
        db,
        admin,
        "broadcast_email_create",
        None,
        {"broadcast_id": broadcast_id, "subject": rendered["subject"]},
    )

    return CreateBroadcastResponse(
        id=broadcast_id,
        status="scheduled",
        recipient_count=len(recipients),
        scheduled_at=req.scheduled_at,
    )


@router.get("/admin/broadcast-email", response_model=ListBroadcastsResponse)
async def list_broadcasts(
    limit: int = 50,
    offset: int = 0,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
):
    """List all broadcast campaigns, newest first."""
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    async with db.pool.acquire() as conn:
        total_row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM email_broadcasts")
        total = total_row["cnt"] if total_row else 0
        rows = await conn.fetch(
            """
            SELECT id, subject, status, recipient_count, scheduled_at, sent_at,
                   created_by, created_at
            FROM email_broadcasts
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )

    broadcasts = [
        BroadcastListItem(
            id=r["id"],
            subject=r["subject"],
            status=r["status"],
            recipient_count=r["recipient_count"],
            scheduled_at=r["scheduled_at"],
            sent_at=r["sent_at"],
            created_by=r["created_by"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return ListBroadcastsResponse(total=total, broadcasts=broadcasts)


@router.get("/admin/broadcast-email/{broadcast_id}", response_model=BroadcastDetailResponse)
async def get_broadcast_detail(
    broadcast_id: str,
    limit: int = 100,
    offset: int = 0,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
):
    """Get broadcast details with per-recipient status (paginated)."""
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, subject, status, recipient_count, scheduled_at, sent_at,
                   created_by, created_at
            FROM email_broadcasts WHERE id = $1
            """,
            broadcast_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Broadcast not found")

        total_row = await conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM email_broadcast_recipients WHERE broadcast_id = $1",
            broadcast_id,
        )
        total_recipients = total_row["cnt"] if total_row else 0

        recipient_rows = await conn.fetch(
            """
            SELECT user_id, email, status, error, sent_at
            FROM email_broadcast_recipients
            WHERE broadcast_id = $1
            ORDER BY id
            LIMIT $2 OFFSET $3
            """,
            broadcast_id,
            limit,
            offset,
        )

    broadcast = BroadcastListItem(
        id=row["id"],
        subject=row["subject"],
        status=row["status"],
        recipient_count=row["recipient_count"],
        scheduled_at=row["scheduled_at"],
        sent_at=row["sent_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )
    recipients = [
        BroadcastRecipientItem(
            user_id=r["user_id"],
            email=r["email"],
            status=r["status"],
            error=r["error"],
            sent_at=r["sent_at"],
        )
        for r in recipient_rows
    ]
    return BroadcastDetailResponse(
        broadcast=broadcast,
        recipients=recipients,
        total_recipients=total_recipients,
    )


@router.delete("/admin/broadcast-email/{broadcast_id}")
async def cancel_broadcast(
    broadcast_id: str,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
):
    """Cancel a scheduled broadcast. Returns 409 if not in 'scheduled' status."""
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM email_broadcasts WHERE id = $1", broadcast_id)
        if not row:
            raise HTTPException(status_code=404, detail="Broadcast not found")
        if row["status"] != "scheduled":
            raise HTTPException(
                status_code=409,
                detail=f"Cannot cancel broadcast with status '{row['status']}'",
            )
        await conn.execute(
            "UPDATE email_broadcasts SET status = 'cancelled' WHERE id = $1", broadcast_id
        )

    cancel_broadcast_job(broadcast_id)
    await log_admin_action(
        db, admin, "broadcast_email_cancel", None, {"broadcast_id": broadcast_id}
    )
    return {"message": "Broadcast cancelled"}


@router.get("/admin/export/requests")
async def admin_export_requests(
    start_time: datetime,
    end_time: datetime | None = None,
    user_id: str | None = None,
    model_id: str | None = None,
    errors_only: bool = False,
    include_content: bool = False,
    admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> StreamingResponse:
    """Stream all request logs matching the given filters as JSONL.

    Query Parameters:
    - start_time: ISO8601 datetime, inclusive lower bound (required)
    - end_time: ISO8601 datetime, inclusive upper bound (defaults to now)
    - user_id: Filter by user ID
    - model_id: Filter by model ID
    - errors_only: If true, only include requests with errors
    - include_content: If true, include prompt and response fields

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    if end_time is None:
        end_time = datetime.now(timezone.utc)

    where_clauses: list[str] = ["l.timestamp >= $1", "l.timestamp <= $2"]
    params: list[Any] = [start_time, end_time]

    if user_id:
        where_clauses.append(f"l.user_id = ${len(params) + 1}")
        params.append(user_id)

    if model_id:
        where_clauses.append(f"l.model_id = ${len(params) + 1}")
        params.append(model_id)

    if errors_only:
        where_clauses.append(
            "(l.error IS NOT NULL OR l.status_code IS NULL "
            "OR l.status_code < 200 OR l.status_code >= 400)"
        )

    content_cols = ", l.prompt, l.response" if include_content else ""
    batch_size = 500

    start_str = start_time.strftime("%Y%m%d")
    end_str = end_time.strftime("%Y%m%d")
    filename = f"requests-{start_str}-{end_str}.jsonl"

    async def generate() -> AsyncGenerator[str, None]:
        cursor_ts: datetime | None = None
        cursor_id: str | None = None
        async with db_logger.pool.acquire() as conn:
            while True:
                local_clauses = list(where_clauses)
                local_params = list(params)
                if cursor_ts is not None:
                    cursor_ts_idx = len(local_params) + 1
                    cursor_id_idx = len(local_params) + 2
                    local_clauses.append(
                        f"(l.timestamp, l.request_id) < (${cursor_ts_idx}, ${cursor_id_idx})"
                    )
                    local_params.append(cursor_ts)
                    local_params.append(cursor_id)
                limit_idx = len(local_params) + 1
                local_where = "WHERE " + " AND ".join(local_clauses)
                rows = await conn.fetch(
                    f"""
                    SELECT
                        l.request_id, l.user_id, u.user_name, u.email AS user_email,
                        l.model_id, l.provider, l.timestamp,
                        l.status_code, l.latency_ms, l.ttft_ms,
                        l.prompt_tokens, l.completion_tokens, l.reasoning_tokens,
                        l.cache_read_tokens, l.cache_write_tokens, l.total_tokens,
                        l.cost_usd, l.error{content_cols}
                    FROM api_logs l
                    LEFT JOIN users u ON u.id = l.user_id
                    {local_where}
                    ORDER BY l.timestamp DESC, l.request_id DESC
                    LIMIT ${limit_idx}
                    """,
                    *local_params,
                    batch_size,
                )
                if not rows:
                    break
                for row in rows:
                    record: dict[str, Any] = {
                        "request_id": row["request_id"],
                        "timestamp": row["timestamp"].isoformat(),
                        "user_id": row["user_id"],
                        "user_name": row["user_name"],
                        "user_email": row["user_email"],
                        "model_id": row["model_id"],
                        "provider": row["provider"],
                        "ttft_ms": row["ttft_ms"],
                        "latency_ms": row["latency_ms"],
                        "prompt_tokens": row["prompt_tokens"],
                        "completion_tokens": row["completion_tokens"],
                        "reasoning_tokens": row["reasoning_tokens"],
                        "cache_read_tokens": row["cache_read_tokens"],
                        "cache_write_tokens": row["cache_write_tokens"],
                        "total_tokens": row["total_tokens"],
                        "cost_usd": (str(row["cost_usd"]) if row["cost_usd"] is not None else None),
                        "status_code": row["status_code"],
                        "error": row["error"],
                    }
                    if include_content:
                        record["prompt"] = row["prompt"]
                        record["response"] = row["response"]
                    yield json.dumps(record) + "\n"
                if len(rows) < batch_size:
                    break
                cursor_ts = rows[-1]["timestamp"]
                cursor_id = rows[-1]["request_id"]

        await log_admin_action(
            db_logger,
            admin_id,
            "export_requests",
            None,
            {
                "range": f"{start_str}-{end_str}",
                "include_content": include_content,
                "user_id": user_id,
                "model_id": model_id,
                "errors_only": errors_only,
            },
        )

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/admin/provider-quotas", response_model=AdminProviderQuotasResponse)
async def admin_provider_quotas(
    _admin_id: str = Depends(verify_admin_access),
) -> AdminProviderQuotasResponse:
    """Return current quota status for each upstream LLM provider."""
    providers = await gather_all()
    return AdminProviderQuotasResponse(
        generated_at=datetime.now(timezone.utc),
        providers=providers,
    )


_PROVIDER_STATS_MAX_DAYS = 90
_PROVIDER_STATS_DEFAULT_DAYS = 7


@router.get("/admin/api/provider-stats", response_model=ProviderStatsResponse)
async def admin_provider_stats(
    request: Request,
    provider: str,
    model_id: str,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> ProviderStatsResponse:
    """Return hourly performance stats for a (provider, model_id) window.

    Query Parameters:
        provider: Required upstream provider key (e.g. ``openrouter``).
        model_id: Required model identifier (e.g. ``qwen/qwen3-coder``).
        from: ISO8601 lower bound (inclusive). Defaults to ``to - 7 days``.
        to:   ISO8601 upper bound (exclusive). Defaults to current hour.

    The window is hour-truncated and capped at 90 days. The response also
    includes the distinct providers and models seen in the window so the UI
    can populate dropdowns from a single round-trip.
    """
    del request  # accepted to match other admin handlers; pool comes from Depends
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=503, detail="database unavailable")

    now = datetime.now(timezone.utc)
    if to is not None:
        to = _require_aware_utc(to, "to")
    if from_ is not None:
        from_ = _require_aware_utc(from_, "from")

    end = _truncate_hour(to) if to else _truncate_hour(now)
    start = _truncate_hour(from_) if from_ else end - timedelta(days=_PROVIDER_STATS_DEFAULT_DAYS)

    if end <= start:
        raise HTTPException(status_code=400, detail="`to` must be after `from`")
    if (end - start) > timedelta(days=_PROVIDER_STATS_MAX_DAYS):
        raise HTTPException(
            status_code=400,
            detail=f"range must be <= {_PROVIDER_STATS_MAX_DAYS} days",
        )

    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT hour_bucket, provider, model_id,
                   request_count, error_count, stream_count,
                   ttft_p50_ms, ttft_p95_ms, ttft_p99_ms,
                   latency_p50_ms, latency_p95_ms, latency_p99_ms,
                   throughput_avg_tps, throughput_p50_tps, throughput_p95_tps,
                   prompt_tokens_avg, completion_tokens_avg, total_completion_tokens
              FROM provider_hourly_stats
             WHERE provider = $1 AND model_id = $2
               AND hour_bucket >= $3 AND hour_bucket < $4
             ORDER BY hour_bucket ASC
            """,
            provider,
            model_id,
            start,
            end,
        )
        providers = await conn.fetch(
            """
            SELECT DISTINCT provider FROM provider_hourly_stats
             WHERE hour_bucket >= $1 AND hour_bucket < $2
             ORDER BY provider
            """,
            start,
            end,
        )
        models = await conn.fetch(
            """
            SELECT DISTINCT model_id FROM provider_hourly_stats
             WHERE hour_bucket >= $1 AND hour_bucket < $2
             ORDER BY model_id
            """,
            start,
            end,
        )
        pairs = await conn.fetch(
            """
            SELECT DISTINCT provider, model_id FROM provider_hourly_stats
             WHERE hour_bucket >= $1 AND hour_bucket < $2
             ORDER BY provider, model_id
            """,
            start,
            end,
        )

    return ProviderStatsResponse(
        rows=[ProviderStatsRow(**dict(r)) for r in rows],
        providers=[r["provider"] for r in providers],
        models=[r["model_id"] for r in models],
        pairs=[ProviderModelPair(provider=r["provider"], model_id=r["model_id"]) for r in pairs],
    )


# ============================================================
# Token Usage tab — per (provider, model_id) totals over a fixed-window
# selector (1h | 24h | 7d | 30d). Reads pre-aggregated rows from
# provider_hourly_stats; no scan of api_logs.
# ============================================================

_TOKEN_USAGE_RANGES: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# Matches the CronTrigger(minute=5) of the rollup_provider_stats job in
# serving/admin/provider_stats_rollup.py — the most recent hour bucket
# is not guaranteed to exist until this many minutes past the hour.
_ROLLUP_MINUTE_OFFSET = 5


@router.get("/admin/api/provider-token-usage", response_model=ProviderTokenUsageResponse)
async def admin_provider_token_usage(
    request: Request,
    range: Literal["1h", "24h", "7d", "30d"] = "24h",
    _admin_id: str = Depends(verify_admin_access),
    db_logger=Depends(get_db_logger),
) -> ProviderTokenUsageResponse:
    """Per-(provider, model_id) token totals + cost over a fixed window.

    Query parameters:
        range: one of "1h", "24h", "7d", "30d". Defaults to "24h".

    The window is hour-truncated; `from = floor(now, hour) - <range>`,
    `to = floor(now, hour)`. Rows are sorted by total token sum
    (input + output + cached + reasoning) descending. Totals are
    summed in Python from the same rows to avoid a second DB hit.
    """
    del request
    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=503, detail="database unavailable")

    delta = _TOKEN_USAGE_RANGES[range]
    # Rollup runs at minute :05, so during [HH:00, HH:05) the bucket for
    # hour HH has not been written yet. Subtract one hour from `end` in
    # that window so we don't undercount and so `refreshed_at` reflects
    # the most recent bucket guaranteed to exist.
    now = datetime.now(timezone.utc)
    end = _truncate_hour(now)
    if now.minute < _ROLLUP_MINUTE_OFFSET:
        end = end - timedelta(hours=1)
    start = end - delta

    async with db_logger.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                provider,
                model_id,
                COALESCE(SUM(total_prompt_tokens), 0)::BIGINT      AS input_tokens,
                COALESCE(SUM(total_completion_tokens), 0)::BIGINT  AS output_tokens,
                COALESCE(SUM(total_cache_read_tokens), 0)::BIGINT  AS cached_tokens,
                COALESCE(SUM(total_reasoning_tokens), 0)::BIGINT   AS reasoning_tokens,
                COALESCE(SUM(total_cost_usd), 0)                   AS cost_usd,
                COALESCE(SUM(request_count), 0)::BIGINT            AS request_count
            FROM provider_hourly_stats
            WHERE hour_bucket >= $1 AND hour_bucket < $2
            GROUP BY provider, model_id
            ORDER BY (
                  COALESCE(SUM(total_prompt_tokens), 0)
                + COALESCE(SUM(total_completion_tokens), 0)
                + COALESCE(SUM(total_cache_read_tokens), 0)
                + COALESCE(SUM(total_reasoning_tokens), 0)
            ) DESC
            """,
            start,
            end,
        )

    out_rows = [ProviderTokenUsageRow(**dict(r)) for r in rows]
    totals = ProviderTokenUsageTotals(
        input_tokens=sum(r.input_tokens for r in out_rows),
        output_tokens=sum(r.output_tokens for r in out_rows),
        cached_tokens=sum(r.cached_tokens for r in out_rows),
        reasoning_tokens=sum(r.reasoning_tokens for r in out_rows),
        cost_usd=sum(r.cost_usd for r in out_rows),
        request_count=sum(r.request_count for r in out_rows),
    )

    return ProviderTokenUsageResponse(
        range=range,
        window=ProviderTokenUsageWindow.model_validate({"from": start, "to": end}),
        refreshed_at=end,
        rows=out_rows,
        totals=totals,
    )


# ========================================
# Signup Domain Allowlist (admin-editable approval policy)
# ========================================


# Domain label charset; matches RFC-1035 LDH plus the dot separator. Each
# label must start and end with an alphanumeric character (no leading or
# trailing hyphens), with optional alphanumeric/hyphen characters in between.
# The TLD must be at least two alpha-only characters.
_DOMAIN_RE = re.compile(r"^([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def _normalize_signup_domain(raw: str) -> tuple[str, bool]:
    """Validate and normalize an admin-supplied signup domain entry.

    Strips whitespace, lowercases, peels a leading ``*.`` to flag wildcard
    intent, and enforces the LDH-plus-dot syntax. Raises ``HTTPException(400)``
    on any validation failure.

    Returns ``(domain_without_prefix, is_wildcard)``.
    """
    cleaned = (raw or "").strip().lower()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Domain is required")

    is_wildcard = False
    if cleaned.startswith("*."):
        is_wildcard = True
        cleaned = cleaned[2:]

    if not cleaned:
        raise HTTPException(
            status_code=400,
            detail="Wildcard entry requires a suffix after '*.' (e.g. *.example.com).",
        )

    # Reject any remaining wildcard / sentinel chars or whitespace.
    for bad in ("*", "@", " ", "\t"):
        if bad in cleaned:
            raise HTTPException(
                status_code=400,
                detail="Domain must not contain '*', '@', or whitespace.",
            )

    if not _DOMAIN_RE.match(cleaned):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid domain format. Use e.g. 'example.com' or "
                "'*.example.com' (lowercase letters, digits, hyphens; "
                "TLD at least 2 letters)."
            ),
        )

    return cleaned, is_wildcard


def _signup_domain_to_schema(row: dict[str, Any]) -> SignupAllowedDomain:
    """Convert a store row to the response schema."""
    return SignupAllowedDomain(
        domain=row["domain"],
        is_wildcard=bool(row.get("is_wildcard")),
        created_at=row.get("created_at"),
        created_by=row.get("created_by"),
        created_by_email=row.get("created_by_email"),
    )


@router.get("/admin/signup-domains", response_model=ListSignupAllowedDomainsResponse)
async def list_signup_allowed_domains_endpoint(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListSignupAllowedDomainsResponse:
    """List all allowed signup domains.

    Empty list means all signups auto-approve. Otherwise only listed
    domains (exact match or ``*.suffix`` wildcard) auto-approve; everyone
    else lands in ``pending_approval``.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rows = await op_store.list_signup_allowed_domains()
    return ListSignupAllowedDomainsResponse(domains=[_signup_domain_to_schema(r) for r in rows])


@router.post(
    "/admin/signup-domains",
    response_model=SignupAllowedDomain,
    status_code=201,
)
async def add_signup_allowed_domain_endpoint(
    request: Request,
    payload: AddSignupAllowedDomainRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> SignupAllowedDomain:
    r"""Add a domain (or ``*.subdomain`` wildcard) to the signup allowlist.

    Validation: strip + lowercase, ``*.`` prefix flips ``is_wildcard``,
    remainder must match ``^([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$``
    (each label must start and end with an alphanumeric character).

    Returns 409 if the (domain, is_wildcard) composite key already exists.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    domain, is_wildcard = _normalize_signup_domain(payload.domain)

    # Resolve admin user id when JWT auth was used (admin_id is the email
    # in that case). Fall back to None for ADMIN_TOKEN where there's no
    # corresponding users row.
    created_by: str | None = None
    user_row = await op_store.get_user_by_email(admin_id) if "@" in admin_id else None
    if user_row:
        created_by = user_row["id"]

    # Translate dup-key violations to 409. We rely solely on asyncpg's typed
    # UniqueViolationError so unrelated DB errors (FK violations, syntax
    # errors that happen to mention the word "constraint", etc.) surface
    # as 500 instead of being silently masked as duplicates. D1 backends
    # that surface duplicates via untyped exceptions will propagate as 500;
    # store implementations that want 409 semantics on D1 should raise a
    # typed exception we recognize here.
    try:
        row = await op_store.add_signup_allowed_domain(
            domain=domain,
            is_wildcard=is_wildcard,
            created_by=created_by,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Domain '{domain}' "
                f"({'wildcard' if is_wildcard else 'exact'}) is already on the allowlist."
            ),
        ) from exc

    invalidate_allowlist_cache()

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "signup_domain.add",
        None,
        {"domain": domain, "is_wildcard": is_wildcard},
    )

    return _signup_domain_to_schema(row)


@router.delete("/admin/signup-domains/{domain}", status_code=204)
async def remove_signup_allowed_domain_endpoint(
    request: Request,
    domain: str,
    wildcard: bool = Query(False, description="True iff removing a *.suffix entry"),
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> Response:
    """Remove a domain from the signup allowlist.

    The ``wildcard`` query param disambiguates the composite key: an
    entry added as ``example.com`` (exact) and ``*.example.com``
    (wildcard) coexist as two rows. Pass ``wildcard=true`` to delete
    the wildcard row, ``wildcard=false`` (default) for the exact row.

    Returns 204 on success, 404 if the row doesn't exist.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    normalized = (domain or "").strip().lower()
    if not normalized:
        raise HTTPException(status_code=400, detail="Domain is required")

    removed = await op_store.remove_signup_allowed_domain(
        domain=normalized,
        is_wildcard=bool(wildcard),
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Domain '{normalized}' "
                f"({'wildcard' if wildcard else 'exact'}) is not on the allowlist."
            ),
        )

    invalidate_allowlist_cache()

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "signup_domain.remove",
        None,
        {"domain": normalized, "is_wildcard": bool(wildcard)},
    )

    return Response(status_code=204)
