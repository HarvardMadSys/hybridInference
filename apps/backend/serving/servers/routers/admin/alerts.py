"""Admin Slack-alert control endpoints (global snooze, per-type mutes)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.observability.alert_mutes import (
    AlertMute,
    list_mutes,
    mute_alert_type,
    unmute_alert_type,
)
from serving.observability.alert_snooze import (
    clear_snooze,
    get_snooze_until,
    set_snooze_until,
)
from serving.observability.alert_types import ALERT_TYPES, AlertType, get_alert_type
from serving.schemas_admin import (
    AlertMuteListResponse,
    AlertSnoozeStatus,
    AlertTypeMuteStatus,
    MuteAlertTypeRequest,
    SnoozeAlertsRequest,
)
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


def _mute_status(alert_type: AlertType, mute: AlertMute | None, now: float) -> AlertTypeMuteStatus:
    """Describe one alert type and the mute on it, if that mute is still active."""
    active = mute if mute is not None and mute.is_active(now) else None
    return AlertTypeMuteStatus(
        alert_type=alert_type.id,
        label=alert_type.label,
        description=alert_type.description,
        group=alert_type.group,
        key_pattern=alert_type.key_pattern,
        muted=active is not None,
        muted_until=active.until if active else None,
        muted_by=active.muted_by if active else None,
        muted_at=active.muted_at if active else None,
    )


def _catalog_entry(alert_type: str) -> AlertType:
    """Resolve a path parameter to its catalog entry, or 404.

    Only catalogued types can be muted: the mute is matched against the prefix
    of a dedupe key, so a typo stored here would silence nothing while reading
    as though it did.
    """
    entry = get_alert_type(alert_type)
    if entry is None:
        raise HTTPException(404, f"Unknown alert type: {alert_type}")
    return entry


@router.get("/alerts/mutes", response_model=AlertMuteListResponse)
async def list_alert_mutes_endpoint(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> AlertMuteListResponse:
    """List every alert type the gateway sends, and which of them are muted.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    mutes = await list_mutes()
    now = time.time()
    return AlertMuteListResponse(
        types=[_mute_status(entry, mutes.get(entry.id), now) for entry in ALERT_TYPES]
    )


@router.put("/alerts/mutes/{alert_type}", response_model=AlertTypeMuteStatus)
async def mute_alert_type_endpoint(
    alert_type: str,
    request: Request,
    payload: MuteAlertTypeRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> AlertTypeMuteStatus:
    """Mute one alert type for ``duration_seconds``, or until unmuted when null.

    Replaces any mute already on the type. Other types, and the global snooze,
    are unaffected.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    entry = _catalog_entry(alert_type)

    now = time.time()
    until = now + payload.duration_seconds if payload.duration_seconds is not None else None
    mute = await mute_alert_type(entry.id, until, admin_id)

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "alerts.mute",
        None,
        {
            "alert_type": entry.id,
            "duration_seconds": payload.duration_seconds,
            "muted_until": until,
        },
    )

    return _mute_status(entry, mute, now)


@router.delete("/alerts/mutes/{alert_type}", response_model=AlertTypeMuteStatus)
async def unmute_alert_type_endpoint(
    alert_type: str,
    request: Request,
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> AlertTypeMuteStatus:
    """Lift the mute on one alert type. A no-op for a type that is not muted.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    entry = _catalog_entry(alert_type)

    removed = await unmute_alert_type(entry.id)

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "alerts.unmute",
        None,
        {"alert_type": entry.id, "removed": removed},
    )

    return _mute_status(entry, None, time.time())
