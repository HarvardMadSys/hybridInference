"""Admin export endpoints — JSONL streaming of request logs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from serving.servers.auth import log_admin_action
from serving.servers.deps import get_db_logger, verify_admin_access
from serving.servers.routers.admin._common import _escape_ilike_substring_term
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)

router = APIRouter(prefix="/admin")


def _decode_jsonish(value: Any) -> Any:
    """Return JSONB values as objects even when asyncpg gives us strings."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        logger.warning("admin export: malformed JSONB string, returning raw")
        return value


@router.get("/export/requests")
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
    - user_id: Filter by user ID, name, or email (substring match)
    - model_id: Filter by model ID (substring match)
    - errors_only: If true, only include requests with errors
    - include_content: If true, include prompt and response fields

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not db_logger or not db_logger.pool:
        raise HTTPException(500, "Database not configured")

    if start_time.tzinfo is None:
        raise HTTPException(422, "start_time must be timezone-aware (e.g. 2024-01-01T00:00:00Z)")

    if end_time is None:
        end_time = datetime.now(timezone.utc)
    elif end_time.tzinfo is None:
        raise HTTPException(422, "end_time must be timezone-aware (e.g. 2024-01-01T00:00:00Z)")

    where_clauses: list[str] = ["l.timestamp >= $1", "l.timestamp <= $2"]
    params: list[Any] = [start_time, end_time]

    # Filters mirror /admin/recent-requests so the JSONL export matches exactly
    # what the admin sees in the Requests tab (which reuses the same filter
    # values): a substring match across user id/name/email, and a substring
    # match on model id. The SELECT below already joins users.
    if user_id:
        params.append(_escape_ilike_substring_term(user_id))
        idx = len(params)
        where_clauses.append(
            f"(l.user_id ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR u.user_name ILIKE '%' || ${idx} || '%' ESCAPE '\\' "
            f"OR u.email ILIKE '%' || ${idx} || '%' ESCAPE '\\')"
        )

    if model_id:
        params.append(_escape_ilike_substring_term(model_id))
        where_clauses.append(f"l.model_id ILIKE '%' || ${len(params)} || '%' ESCAPE '\\'")

    if errors_only:
        where_clauses.append(
            "(l.error IS NOT NULL OR l.status_code IS NULL "
            "OR l.status_code < 200 OR l.status_code >= 400)"
        )

    content_cols = (
        ", l.prompt, l.response, l.tools, l.metadata, l.request_payload" if include_content else ""
    )
    batch_size = 500

    start_str = start_time.strftime("%Y%m%d")
    end_str = end_time.strftime("%Y%m%d")
    filename = f"requests-{start_str}-{end_str}.jsonl"

    async def generate() -> AsyncGenerator[str, None]:
        cursor_ts: datetime | None = None
        cursor_id: str | None = None
        try:
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
                            "cost_usd": (
                                str(row["cost_usd"]) if row["cost_usd"] is not None else None
                            ),
                            "status_code": row["status_code"],
                            "error": row["error"],
                        }
                        if include_content:
                            record["prompt"] = row["prompt"]
                            record["response"] = row["response"]
                            record["tools"] = _decode_jsonish(row["tools"])
                            record["metadata"] = _decode_jsonish(row["metadata"])
                            record["request_payload"] = _decode_jsonish(row["request_payload"])
                        yield json.dumps(record) + "\n"
                    if len(rows) < batch_size:
                        break
                    cursor_ts = rows[-1]["timestamp"]
                    cursor_id = rows[-1]["request_id"]
        finally:
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
