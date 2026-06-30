"""Admin broadcast-email endpoints."""

from __future__ import annotations

import asyncio as _asyncio
import json as _json
import uuid as _uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException

from serving.schemas_admin import (
    BroadcastDetailResponse,
    BroadcastListItem,
    BroadcastPreviewRequest,
    BroadcastPreviewResponse,
    BroadcastRecipientItem,
    CreateBroadcastRequest,
    CreateBroadcastResponse,
    ListBroadcastsResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_db_logger, verify_admin_access
from serving.utils.email import render_broadcast_template, send_email
from serving.utils.email_scheduler import cancel_broadcast_job, schedule_broadcast

if TYPE_CHECKING:
    from decimal import Decimal

router = APIRouter(prefix="/admin")


def _recipient_where(
    target_roles: list[str],
    target_statuses: list[str],
    min_spend_today_usd: Decimal | None,
) -> tuple[str, list[Any]]:
    """Build the recipient WHERE clause and bound params for a broadcast.

    Shared by the preview COUNT and the create-time snapshot so the audience an
    admin previews can't drift from who actually gets the email. The optional
    spend gate reads today's (UTC) cost from the ``user_daily_cost`` counter
    table; users with no row today count as $0 and are filtered out.
    """
    clauses = ["role = ANY($1::text[])", "status = ANY($2::text[])"]
    params: list[Any] = [target_roles or [], target_statuses or []]
    if min_spend_today_usd is not None:
        params.append(min_spend_today_usd)
        clauses.append(
            "COALESCE((SELECT udc.cost_usd FROM user_daily_cost udc "
            "  WHERE udc.user_id = users.id "
            "    AND udc.day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')), 0) "
            f"> ${len(params)}"
        )
    return " AND ".join(clauses), params


def _render_or_422(req: BroadcastPreviewRequest) -> dict[str, str]:
    """Render template (or pass through custom content), mapping ValueError to 422."""
    try:
        return render_broadcast_template(
            req.template_key,
            req.template_vars,
            custom_subject=req.subject,
            custom_body_html=req.body_html,
            custom_body_text=req.body_text,
            custom_body_markdown=req.body_markdown,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/broadcast-email/preview", response_model=BroadcastPreviewResponse)
async def preview_broadcast(
    req: BroadcastPreviewRequest,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
):
    """Return recipient count and rendered email preview without sending."""
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    rendered = _render_or_422(req)

    where_sql, params = _recipient_where(
        req.target_roles, req.target_statuses, req.min_spend_today_usd
    )
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT COUNT(*) as cnt FROM users WHERE {where_sql}",
            *params,
        )
    count = row["cnt"] if row else 0

    return BroadcastPreviewResponse(
        recipient_count=count,
        rendered_subject=rendered["subject"],
        rendered_body_html=rendered["body_html"],
        rendered_body_text=rendered["body_text"],
    )


@router.post("/broadcast-email/test")
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


@router.post("/broadcast-email", response_model=CreateBroadcastResponse)
async def create_broadcast(
    req: CreateBroadcastRequest,
    admin: str = Depends(verify_admin_access),
    db=Depends(get_db_logger),
):
    """Create a broadcast, snapshot its recipients, and schedule (or fire immediately)."""
    if not db or not db.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    rendered = _render_or_422(req)
    if not rendered["subject"] or not rendered["body_html"]:
        raise HTTPException(status_code=422, detail="subject and body_html are required")

    broadcast_id = str(_uuid.uuid4())
    # Serialize before opening the transaction so we hold the DB connection
    # for the minimum time. ensure_ascii=False keeps unicode (emoji, non-English
    # text) as UTF-8 in the JSONB column rather than \uXXXX escapes.
    template_vars_json = _json.dumps(req.template_vars, ensure_ascii=False)

    async with db.pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
                INSERT INTO email_broadcasts
                    (id, subject, body_html, body_text, template_key, template_vars,
                     target_roles, target_statuses, status, scheduled_at, created_by)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11)
                """,
            broadcast_id,
            rendered["subject"],
            rendered["body_html"],
            rendered["body_text"],
            req.template_key,
            template_vars_json,
            req.target_roles or [],
            req.target_statuses or [],
            "scheduled",
            req.scheduled_at,
            admin,
        )

        # Snapshot recipients at create time so the audience matches what the admin
        # previewed and can't drift between scheduling and execution.
        where_sql, params = _recipient_where(
            req.target_roles, req.target_statuses, req.min_spend_today_usd
        )
        recipients = await conn.fetch(
            f"SELECT id, email FROM users WHERE {where_sql}",
            *params,
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


@router.get("/broadcast-email", response_model=ListBroadcastsResponse)
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


@router.get("/broadcast-email/{broadcast_id}", response_model=BroadcastDetailResponse)
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


@router.delete("/broadcast-email/{broadcast_id}")
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
