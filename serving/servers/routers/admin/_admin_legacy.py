"""Admin API endpoints for user and system management."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
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
    AdminProviderQuotasResponse,
    BroadcastDetailResponse,
    BroadcastListItem,
    BroadcastPreviewRequest,
    BroadcastPreviewResponse,
    BroadcastRecipientItem,
    CreateBroadcastRequest,
    CreateBroadcastResponse,
    ListBroadcastsResponse,
    ListSignupAllowedDomainsResponse,
    ProviderModelPair,
    ProviderStatsResponse,
    ProviderStatsRow,
    ProviderTokenUsageResponse,
    ProviderTokenUsageRow,
    ProviderTokenUsageTotals,
    ProviderTokenUsageWindow,
    SignupAllowedDomain,
)
from serving.servers.auth import (
    log_admin_action,
)
from serving.servers.deps import (
    get_db_logger,
    get_operational_store,
    verify_admin_access,
)
from serving.servers.routers.admin._common import (
    _require_aware_utc,
    _truncate_hour,
)
from serving.utils.email import render_broadcast_template, send_email
from serving.utils.email_scheduler import cancel_broadcast_job, schedule_broadcast
from serving.utils.request_ip import get_client_ip

router = APIRouter()

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
