"""Admin endpoint to purge ``login_events`` rows.

Two valid query shapes (exactly one must be supplied):

- ``?older_than_days=N`` — purge rows older than ``N`` days (retention).
- ``?user_id=...`` — purge all rows for one user (GDPR-style deletion).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")


@router.delete("/login-events")
async def purge_login_events_endpoint(
    request: Request,
    older_than_days: int | None = Query(None, ge=1),
    user_id: str | None = Query(None, min_length=1),
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> dict[str, int]:
    """Purge ``login_events`` rows by age or by user."""
    if not op_store:
        raise HTTPException(500, "Database not configured")
    if (older_than_days is None) == (user_id is None):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of `older_than_days` or `user_id`",
        )

    if older_than_days is not None:
        deleted = await op_store.purge_login_events_older_than(older_than_days)
        details: dict[str, object] = {
            "older_than_days": older_than_days,
            "deleted": deleted,
        }
    else:
        deleted = await op_store.purge_login_events_for_user(user_id)
        details = {"user_id": user_id, "deleted": deleted}

    ip = get_client_ip(request)
    await log_admin_action(op_store, ip, "login_events.purge", None, details)
    return {"deleted": deleted}
