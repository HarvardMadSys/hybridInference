"""Admin test email: one fixed message through the saved email settings."""

from __future__ import annotations

import asyncio
import math
import time

from fastapi import APIRouter, Depends, HTTPException, Request

# FastAPI resolves the dependency's annotation at runtime.
from serving.audit import AuditActor  # noqa: TC001
from serving.config import app_config
from serving.config.settings import settings
from serving.schemas_admin import TestEmailRequest, TestEmailResponse
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_admin_audit_actor,
    get_admin_user_id,
    get_operational_store,
    verify_admin_access,
)
from serving.utils.email import deliver_email, email_configuration_problem, render_test_email
from serving.utils.logging import get_logger

router = APIRouter(prefix="/admin")
logger = get_logger(__name__)

_TEST_SEND_INTERVAL_SECONDS = 10.0
#: Earliest next test per administrator, on the monotonic clock. Like the alert
#: test's, a guard against a double click or a retry loop, kept in-process.
_test_send_not_before: dict[str, float] = {}


def _release_test_slot(admin_id: str, previous_deadline: float) -> None:
    """Give back a reservation for a test that was never sent, keeping any earlier window."""
    if previous_deadline > time.monotonic():
        _test_send_not_before[admin_id] = previous_deadline
    else:
        _test_send_not_before.pop(admin_id, None)


async def _recipient(payload: TestEmailRequest, request: Request, op_store) -> str:
    """Check the saved settings can send, and return where the test goes.

    Raises:
        HTTPException: 503 when the saved settings cannot be read, 400 when they
            cannot send or there is no address to send to.
    """
    try:
        # The save may have reached another worker: test what is saved, not
        # this worker's copy from before its next periodic refresh.
        await app_config.refresh()
    except Exception:
        logger.warning("email test configuration refresh failed")
        raise HTTPException(503, "Could not read the saved email settings. Try again.") from None
    problem = email_configuration_problem()
    if problem is not None:
        raise HTTPException(400, f"{problem} Save them, then send the test.")
    if payload.to is not None:
        return str(payload.to)
    user_id = get_admin_user_id(request)
    user = None
    if user_id is not None and op_store is not None:
        user = await op_store.get_user_by_id(user_id)
    address = user.get("email") if user else None
    if not address:
        raise HTTPException(
            400, "Your sign-in has no email address. Enter an address to send the test to."
        )
    return address


@router.post("/email/test", response_model=TestEmailResponse)
async def send_test_email(
    payload: TestEmailRequest,
    request: Request,
    admin_id: str = Depends(verify_admin_access),
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> TestEmailResponse:
    """Send the gateway's fixed test message through the saved email settings.

    The message is the gateway's own, so this cannot send arbitrary mail. It
    goes to the address given, or else to the administrator's own. Settings
    that cannot send, or no address to send to, are refused before anything
    is sent and do not hold up the next test.
    """
    now = time.monotonic()
    deadline = _test_send_not_before.get(admin_id, 0.0)
    if now < deadline:
        raise HTTPException(
            429,
            "Wait a few seconds before sending another test email.",
            headers={"Retry-After": str(math.ceil(deadline - now))},
        )
    # Reserved before any await, so a second click cannot slip in while the
    # first is still reading the saved settings.
    _test_send_not_before[admin_id] = now + _TEST_SEND_INTERVAL_SECONDS
    try:
        recipient = await _recipient(payload, request, op_store)
    except HTTPException:
        _release_test_slot(admin_id, deadline)
        raise

    provider = settings.email_provider
    subject, html_body, text_body = render_test_email(provider)
    # Delivery is sync; offload it so the event loop keeps serving.
    delivery = await asyncio.to_thread(deliver_email, recipient, subject, html_body, text_body)
    try:
        await log_admin_action(
            op_store,
            action="email.test",
            details={"provider": provider, "to": recipient, "sent": delivery.sent},
            success=delivery.sent,
            actor=actor,
        )
    except Exception:
        # A sent message cannot be unsent; report its result even if audit is down.
        logger.warning("email test audit failed: provider=%s sent=%s", provider, delivery.sent)
    if not delivery.sent:
        raise HTTPException(
            502, delivery.reason or "The test email was not sent. The backend log says why."
        )
    return TestEmailResponse(to=recipient, provider=provider)
