"""Cloudflare Email Sending: the gateway's mail through Cloudflare's REST API.

Selected with ``EMAIL_PROVIDER=cloudflare``. Each message the gateway sends --
verification, password reset, approval and rejection notices, signup alerts,
admin broadcasts -- is one ``POST /accounts/{account_id}/email/sending/send``
carrying the bodies the gateway rendered; Cloudflare applies no template. The
sender is ``SMTP_FROM_NAME <SMTP_FROM_EMAIL>``, the gateway's sender identity
whichever transport carries the message. Its domain must be onboarded under
Email Service > Email Sending on the account that owns the token.

``CLOUDFLARE_ACCOUNT_ID`` and ``CLOUDFLARE_EMAIL_API_TOKEN`` are read on every
send, so a stored change or a rotated token applies without a restart. While
either is empty the transport is unconfigured and sends nothing: a selected
provider never falls back to another.

A send is not repeated automatically: Cloudflare may have accepted a request
whose answer was lost.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from serving.config.settings import settings
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.utils.delivery import EmailMessage

logger = get_logger(__name__)

API_BASE_URL = "https://api.cloudflare.com/client/v4"
#: Seconds to wait for Cloudflare's answer; the SMTP transport waits as long.
TIMEOUT_SECONDS = 10.0
#: How many of a refusal's errors the log names.
_ERRORS_LOGGED = 3
#: What a logged error code or message may look like. Cloudflare's are short
#: dotted identifiers (``email.sending.error.throttled``); anything else could
#: echo the request, so it is not repeated.
_SAFE_ERROR_TEXT = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _credentials() -> tuple[str, str]:
    return (
        settings.cloudflare_account_id.strip(),
        settings.cloudflare_email_api_token.strip(),
    )


def is_configured() -> bool:
    """Return whether both the account id and the API token are set."""
    return all(_credentials())


def _describe_errors(errors: Any) -> str:
    """Name a refusal's error codes, never text that could carry the request."""
    described: list[str] = []
    if isinstance(errors, list):
        for error in errors[:_ERRORS_LOGGED]:
            if not isinstance(error, dict):
                continue
            parts = [
                str(part)
                for part in (error.get("code"), error.get("message"))
                if part is not None and _SAFE_ERROR_TEXT.fullmatch(str(part))
            ]
            if parts:
                described.append(" ".join(parts))
    return "; ".join(described) or "no error code"


def send(message: EmailMessage) -> bool:
    """Send one rendered message, returning True once Cloudflare delivered or queued it.

    Never raises: a refusal, a bounce, a suppressed recipient or a failed
    connection is logged and returns False. ``template`` and ``template_data``
    select a template on template transports; Cloudflare receives the rendered
    bodies instead, as SMTP does.
    """
    account_id, token = _credentials()
    if not (account_id and token):
        logger.warning("Email not sent: Cloudflare Email Sending is not configured")
        return False

    sender: dict[str, str] = {"address": settings.smtp_from_email}
    if settings.smtp_from_name:
        sender["name"] = settings.smtp_from_name
    body: dict[str, Any] = {
        "from": sender,
        "to": message.to_email,
        "subject": message.subject,
        "html": message.html_body,
    }
    if message.text_body:
        body["text"] = message.text_body

    url = f"{API_BASE_URL}/accounts/{quote(account_id, safe='')}/email/sending/send"
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.error(
            "Failed to send email to %s through Cloudflare: %s",
            message.to_email,
            type(exc).__name__,
        )
        return False

    try:
        envelope = response.json()
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        errors = envelope.get("errors") if isinstance(envelope, dict) else None
        logger.error(
            "Cloudflare refused email to %s: HTTP %s, %s",
            message.to_email,
            response.status_code,
            _describe_errors(errors),
        )
        return False

    result = envelope.get("result")
    if not isinstance(result, dict):
        result = {}
    if result.get("delivered") or result.get("queued"):
        logger.info("Email sent successfully to %s", message.to_email)
        return True
    # One recipient per request: accepted, but neither delivered nor queued.
    if result.get("permanent_bounces"):
        reason = "the address bounced permanently"
    elif result.get("suppressed_recipients"):
        reason = "the address is on the account's suppression list"
    else:
        reason = "Cloudflare reported neither delivery nor queueing"
    logger.error("Cloudflare did not deliver email to %s: %s", message.to_email, reason)
    return False
