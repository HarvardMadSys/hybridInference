"""Admin endpoints for the auth-failure IP blocklist.

Read and lift the blocks ``utils/auth_failure_blocklist.py`` applies. Both are
deliberately narrow: the blocklist's thresholds are configuration
(``auth_failure_block_*``) and its permanent exemption is
``auth_failure_block_exempt_ips`` -- neither is editable here. What was missing
was any way to see an active block, or to end one before its deadline without
restarting the gateway.

That gap has a specific shape. ``servers/auth.py`` consults the blocklist
*before* it reads the presented key, so a deployment-owned caller whose
credential went stale -- a monitor, a CI job, a service account -- crosses the
threshold, gets refused, and stays refused for the rest of
``auth_failure_block_duration_sec`` (a day by default) even after an operator
fixes the credential. Clearing the block is what closes that window.

**Per process.** The blocklist is in-memory, so these endpoints see and change
only the worker that answers the request. That is exact on the single-process
default (``deploy/systemd/``) and partial on a multi-worker deployment, where
each worker holds its own counts; ``process_scoped`` in the listing response
says so rather than implying a deployment-wide view. Clearing a block on a
multi-worker deployment may need one call per worker.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from serving.config.settings import settings
from serving.schemas_admin import (
    AuthBlockItem,
    ClearAuthBlockRequest,
    ClearAuthBlockResponse,
    ListAuthBlocksResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.auth_failure_blocklist import clear_block, list_active_blocks
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip, normalize_ip_bucket

logger = get_logger(__name__)

router = APIRouter(prefix="/admin")


@router.get("/auth-blocks", response_model=ListAuthBlocksResponse)
async def list_auth_blocks(
    _admin_id: str = Depends(verify_admin_access),
) -> ListAuthBlocksResponse:
    """List the source buckets this worker is currently refusing, longest wait first.

    Needs no database: the blocklist is process memory, so this answers on a
    deployment whose operational store is unavailable -- which is worth having,
    since that is one of the states in which auth starts failing.
    """
    blocks = await list_active_blocks()
    return ListAuthBlocksResponse(
        blocks=[
            AuthBlockItem(
                ip_bucket=block.ip_bucket,
                blocked_until=datetime.fromtimestamp(block.blocked_until, tz=timezone.utc),
                retry_after_sec=block.retry_after_sec,
            )
            for block in blocks
        ],
        enabled=settings.auth_failure_block_enabled,
        process_scoped=True,
    )


@router.post("/auth-blocks/clear", response_model=ClearAuthBlockResponse)
async def clear_auth_block(
    request: Request,
    payload: ClearAuthBlockRequest,
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ClearAuthBlockResponse:
    """Lift one active block on this worker, and drop the bucket's counted history.

    Idempotent, and reports what it found: ``cleared: false`` means there was
    no active block to lift, which is a normal answer (it lapsed, or the bucket
    was never blocked) rather than an error -- so this returns 200 instead of
    404. A caller still presenting a bad key simply accrues failures again and
    is blocked again on crossing the threshold; this grants no immunity, and
    ``auth_failure_block_exempt_ips`` remains the way to grant that.

    Audited, but best-effort — deliberately unlike the bare
    :func:`log_admin_action` call every other admin mutation makes. Those write
    the audit row through the same store the mutation itself went to, so a store
    failure means nothing changed and propagating it is honest. Here the change
    is to process memory and has *already* happened by the time the audit runs:
    letting the write raise would answer 500 for a block that is genuinely
    lifted, and the retry would then report ``cleared: false`` — unblocked, yet
    reading as "nothing was blocked" — in precisely the store outage this
    endpoint has to keep working through. So the lift is reported and the audit
    failure is logged instead of returned. (A store that is merely absent needs
    nothing here: ``log_admin_action`` already returns early for a falsy one.)
    """
    ip_bucket = normalize_ip_bucket(payload.ip)
    cleared = await clear_block(payload.ip)

    try:
        await log_admin_action(
            op_store,
            get_client_ip(request),
            "auth_blocks.clear",
            None,
            {"ip": payload.ip, "ip_bucket": ip_bucket, "cleared": cleared},
        )
    except Exception:
        logger.exception(
            "auth_block_clear_audit_failed",
            extra={
                "event": "auth_block_clear_audit_failed",
                "ip_bucket": ip_bucket,
                "cleared": cleared,
            },
        )

    return ClearAuthBlockResponse(ip_bucket=ip_bucket, cleared=cleared)
