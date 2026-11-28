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
from serving.utils.delivery import EmailDelivery
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
#: Where to look for an expired token, a missing permission or a domain that
#: is not onboarded, by the error code the send endpoint documents
#: (https://developers.cloudflare.com/email-service/api/send-emails/rest-api/).
_ADVICE_BY_CODE: dict[int, str] = {
    10000: "Cloudflare found nothing to send with at this account ID. Check the account ID.",
    10001: "Cloudflare refused the request as malformed. Check the sender address.",
    10002: "Cloudflare had an internal error. Try again in a few minutes.",
    10003: "Cloudflare does not support this request.",
    10004: "Cloudflare is rate-limiting this account. Wait a minute and try again.",
    10100: "Cloudflare's authentication service is unavailable. Try again in a few minutes.",
    10101: (
        "Cloudflare did not accept the API token. Check that it was copied whole and has "
        "not been revoked or expired."
    ),
    10102: (
        "The API token may not send email for this account. Give it the Email Sending: Edit "
        "permission on the account whose ID is configured."
    ),
    10103: (
        "This kind of Cloudflare token cannot send email. Create an API token with the "
        "Email Sending: Edit permission."
    ),
    10105: "This Cloudflare account is not entitled to Email Sending.",
    10200: "The message is larger than Cloudflare accepts.",
    10202: (
        "Cloudflare refused the message as invalid. Check that the sender address is on a "
        "domain onboarded under Email Service > Email Sending."
    ),
    10203: "Email Sending is turned off for this domain or account in Cloudflare.",
    10207: "Email Sending is paused for this Cloudflare account.",
    10208: (
        "This Cloudflare plan sends only to verified destination addresses. Verify the "
        "recipient in Cloudflare first."
    ),
}


def _credentials() -> tuple[str, str]:
    return (
        settings.cloudflare_account_id.strip(),
        settings.cloudflare_email_api_token.strip(),
    )


def is_configured() -> bool:
    """Return whether both the account id and the API token are set."""
    return all(_credentials())


def _describe_errors(errors: Any, *, withhold: tuple[str, ...] = ()) -> str:
    """Name a refusal's error codes, never text that could carry the request.

    A part containing one of *withhold* (the API token) is left out even when
    it looks like an identifier: the reason reaches the admin console, which
    never shows a saved secret.
    """
    described: list[str] = []
    if isinstance(errors, list):
        for error in errors[:_ERRORS_LOGGED]:
            if not isinstance(error, dict):
                continue
            parts = [
                str(part)
                for part in (error.get("code"), error.get("message"))
                if part is not None
                and _SAFE_ERROR_TEXT.fullmatch(str(part))
                and not any(secret and secret in str(part) for secret in withhold)
            ]
            if parts:
                described.append(" ".join(parts))
    return "; ".join(described) or "no error code"


def _advice_by_status(status: int) -> str:
    if status == 401:
        return _ADVICE_BY_CODE[10101]
    if status == 403:
        return (
            "Cloudflare refused to send. Check that the API token has the Email Sending: Edit "
            "permission and that the sender's domain is onboarded for Email Sending."
        )
    if status == 404:
        return _ADVICE_BY_CODE[10000]
    if status == 429:
        return _ADVICE_BY_CODE[10004]
    if status >= 500:
        return "Cloudflare could not send the message just now. Try again in a few minutes."
    if status < 400:
        return f"Cloudflare answered in a way its API does not (HTTP {status}). Try again."
    return (
        "Cloudflare refused the message. Check the sender address and that its domain is "
        "onboarded under Email Service > Email Sending."
    )


def _refusal_reason(status: int, errors: Any, described: str) -> str:
    """Say what to fix: the first documented code's advice, else the status's, then the codes."""
    codes = (
        [error.get("code") for error in errors if isinstance(error, dict)]
        if isinstance(errors, list)
        else []
    )
    advice = next(
        (
            _ADVICE_BY_CODE[code]
            for code in codes
            if isinstance(code, int) and code in _ADVICE_BY_CODE
        ),
        None,
    ) or _advice_by_status(status)
    return f"{advice} (Cloudflare answered HTTP {status}: {described}.)"


def deliver(message: EmailMessage) -> EmailDelivery:
    """Send one rendered message and say what happened.

    Sent means Cloudflare delivered or queued it. Never raises: a refusal, a
    bounce, a suppressed recipient or a failed connection is logged and comes
    back unsent, with the reason an administrator can act on.
    ``template`` and ``template_data`` select a template on template
    transports; Cloudflare receives the rendered bodies instead, as SMTP does.
    """
    account_id, token = _credentials()
    if not (account_id and token):
        logger.warning("Email not sent: Cloudflare Email Sending is not configured")
        return EmailDelivery(
            False, "Cloudflare Email Sending needs both an account ID and an API token."
        )

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
        return EmailDelivery(
            False,
            f"The gateway could not reach Cloudflare's API ({type(exc).__name__}). Check that "
            "it can make outbound HTTPS requests to api.cloudflare.com.",
        )

    try:
        envelope = response.json()
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        errors = envelope.get("errors") if isinstance(envelope, dict) else None
        described = _describe_errors(errors, withhold=(token,))
        logger.error(
            "Cloudflare refused email to %s: HTTP %s, %s",
            message.to_email,
            response.status_code,
            described,
        )
        return EmailDelivery(False, _refusal_reason(response.status_code, errors, described))

    result = envelope.get("result")
    if not isinstance(result, dict):
        result = {}
    if result.get("delivered") or result.get("queued"):
        logger.info("Email sent successfully to %s", message.to_email)
        return EmailDelivery(True)
    # One recipient per request: accepted, but neither delivered nor queued.
    if result.get("permanent_bounces"):
        reason = "the address bounced permanently"
        advice = "Cloudflare accepted the message, but the recipient address bounced permanently."
    elif result.get("suppressed_recipients"):
        reason = "the address is on the account's suppression list"
        advice = (
            "Cloudflare accepted the message, but the recipient address is on the account's "
            "suppression list."
        )
    else:
        reason = "Cloudflare reported neither delivery nor queueing"
        advice = "Cloudflare accepted the message but reported neither delivery nor queueing."
    logger.error("Cloudflare did not deliver email to %s: %s", message.to_email, reason)
    return EmailDelivery(False, advice)


def send(message: EmailMessage) -> bool:
    """Send one rendered message, returning True once Cloudflare delivered or queued it.

    :func:`deliver` also says why a message was not sent.
    """
    return deliver(message).sent
