"""Admin Slack-alert control endpoints (global snooze)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.observability.alert_snooze import (
    clear_snooze,
    get_snooze_until,
    set_snooze_until,
)
from serving.schemas_admin import AlertSnoozeStatus, SnoozeAlertsRequest
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")


def _status(snooze_until: float) -> AlertSnoozeStatus:
    """Build a status payload from a snooze deadline (epoch seconds)."""
    now = time.time()
    snoozed = snooze_until > now
    return AlertSnoozeStatus(
        snoozed=snoozed,
        snooze_until=snooze_until if snoozed else None,
        seconds_remaining=int(snooze_until - now) if snoozed else 0,
    )


@router.get("/alerts/snooze", response_model=AlertSnoozeStatus)
async def get_alert_snooze_endpoint(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> AlertSnoozeStatus:
    """Return whether Slack alerts are currently snoozed and for how long.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    return _status(await get_snooze_until())


@router.post("/alerts/snooze", response_model=AlertSnoozeStatus)
async def snooze_alerts_endpoint(
    request: Request,
    payload: SnoozeAlertsRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> AlertSnoozeStatus:
    """Suppress all Slack alerts for ``duration_seconds`` from now.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    snooze_until = time.time() + payload.duration_seconds
    await set_snooze_until(snooze_until, admin_id)

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "alerts.snooze",
        None,
        {"duration_seconds": payload.duration_seconds, "snooze_until": snooze_until},
    )

    return _status(snooze_until)


@router.delete("/alerts/snooze", response_model=AlertSnoozeStatus)
async def clear_alert_snooze_endpoint(
    request: Request,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> AlertSnoozeStatus:
    """Resume Slack alerting immediately by clearing any active snooze.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    await clear_snooze(admin_id)

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "alerts.snooze_clear",
        None,
        {},
    )

    return _status(0.0)
