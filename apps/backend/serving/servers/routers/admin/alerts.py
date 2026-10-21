"""Admin alert controls and test notification delivery."""

from __future__ import annotations

import logging
import math
import re
import time
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.config import app_config
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
from serving.observability.alerts import AlertSeverity, SlackSink, WeComSink
from serving.schemas_admin import (
    AlertMuteListResponse,
    AlertSnoozeStatus,
    AlertTypeMuteStatus,
    MuteAlertTypeRequest,
    SnoozeAlertsRequest,
    TestAlertRequest,
    TestAlertResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")
logger = logging.getLogger(__name__)
_TEST_SEND_INTERVAL_SECONDS = 10.0
_test_send_not_before = 0.0


def _test_webhook(payload: TestAlertRequest) -> str:
    """Resolve and validate the destination without including it in errors."""
    if payload.webhook_url is not None:
        webhook_url = payload.webhook_url.get_secret_value()
    elif payload.channel == "slack":
        webhook_url = app_config.configured_value("SLACK_ALERTS_WEBHOOK_URL", "") or (
            app_config.configured_value("SLACK_WEBHOOK_URL", "")
        )
    else:
        webhook_url = app_config.configured_value("WECOM_ALERTS_WEBHOOK_URL", "")
    if not webhook_url:
        raise HTTPException(400, "Enter a webhook URL or save one for the selected channel first")
    valid = False
    try:
        parts = urlsplit(webhook_url)
        if (
            len(webhook_url) <= 4096
            and not any(char.isspace() or ord(char) < 32 for char in webhook_url)
            and parts.scheme == "https"
            and "#" not in webhook_url
        ):
            if payload.channel == "slack":
                valid = bool(
                    parts.netloc.lower() in {"hooks.slack.com", "hooks.slack-gov.com"}
                    and "?" not in webhook_url
                    and re.fullmatch(r"/services/[\w-]+/[\w-]+/[\w-]+", parts.path, re.ASCII)
                )
            else:
                valid = bool(
                    parts.netloc.lower() == "qyapi.weixin.qq.com"
                    and parts.path == "/cgi-bin/webhook/send"
                    and re.fullmatch(r"key=[\w-]+", parts.query, re.ASCII)
                )
    except ValueError:
        pass
    if not valid:
        label = "Slack" if payload.channel == "slack" else "WeCom"
        raise HTTPException(400, f"Enter a valid {label} incoming webhook URL")
    return webhook_url


@router.post("/alerts/test", response_model=TestAlertResponse)
async def test_alert_endpoint(
    request: Request,
    payload: TestAlertRequest,
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> TestAlertResponse:
    """Send a marked test directly, bypassing incident suppression and state.

    A supplied URL tests an unsaved draft. Otherwise use the latest saved
    channel configuration, even when normal delivery is awaiting a restart.
    """
    global _test_send_not_before
    if payload.webhook_url is None:
        try:
            # The save may have reached a different worker. Do not test this
            # worker's cached credential while its periodic refresh is pending.
            await app_config.refresh()
        except Exception:
            logger.warning("alert test configuration refresh failed: channel=%s", payload.channel)
            raise HTTPException(503, "Could not read the saved webhook; retry the test") from None
    webhook_url = _test_webhook(payload)
    now = time.monotonic()
    if now < _test_send_not_before:
        raise HTTPException(
            429,
            "Wait a few seconds before sending another test notification",
            headers={"Retry-After": str(math.ceil(_test_send_not_before - now))},
        )
    # No await between the check and reservation; one bounded slot per worker.
    _test_send_not_before = now + _TEST_SEND_INTERVAL_SECONDS
    test_id = f"alert-test-{uuid.uuid4().hex[:12]}"
    sink = SlackSink(webhook_url) if payload.channel == "slack" else WeComSink(webhook_url)
    sent = False
    try:
        message = "[TEST]\n" + sink.render(
            AlertSeverity.INFO,
            "Test notification",
            {
                "test_id": test_id,
                "message": "Sent from Admin Configuration. No alert incident was created.",
            },
        )
        sent = await sink.post(message, dedupe_key=test_id)
    except Exception:
        # Adapter exceptions may contain the credential; retain only safe context.
        logger.warning("alert test failed: channel=%s test_id=%s", payload.channel, test_id)
    try:
        await log_admin_action(
            op_store,
            get_client_ip(request),
            "alerts.test",
            None,
            {"channel": payload.channel, "test_id": test_id, "sent": sent},
            success=sent,
        )
    except Exception:
        # A delivered message cannot be undone; report its result even if audit is down.
        logger.warning("alert test audit failed: channel=%s test_id=%s", payload.channel, test_id)
    if not sent:
        raise HTTPException(502, "Test notification could not be delivered; check the webhook")
    return TestAlertResponse(channel=payload.channel, test_id=test_id)


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
    """Return whether alerts are currently snoozed and for how long.

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
    """Suppress all alerts for ``duration_seconds`` from now.

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
    """Resume alerting immediately by clearing any active snooze.

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
