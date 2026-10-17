"""Confirm that a signed-in account's owner is present before a sensitive change.

A bearer token proves a session, not a person: a token lifted from a shared
computer or a script on the page holds it for its lifetime. Deleting the
account, changing its sign-in email and giving it a first password each ask
again, the way the account signs in -- its password, or a code sent to its
verified phone when it has none.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from serving.schemas_auth import PhoneVerifyRequest
from serving.servers.routers.phone_auth_routes import verify_phone_code
from serving.utils import password as password_utils
from serving.utils.login_rate_limit import check_and_record_login
from serving.utils.request_ip import get_client_ip_info
from serving.utils.sms import is_sms_enabled


def account_identifier(user_row: dict[str, Any]) -> str | None:
    """The name an account signs in with: its email, else login name, else phone."""
    return user_row.get("email") or user_row.get("login_name") or user_row.get("phone")


async def confirm_account_owner(
    request: Request,
    op_store: Any,
    user_row: dict[str, Any],
    *,
    password: str | None,
    phone_code: str | None,
    purpose: str,
) -> None:
    """Raise unless *password* or *phone_code* proves the owner of *user_row*.

    An account with a password must give it; attempts count against the login
    rate limit, so this is never a second place to guess it. An account
    without one must give a code for *purpose* sent to its verified phone.

    Raises:
        HTTPException: 400 for a wrong or missing proof, or an account with no
            way to give one; 429 when rate-limited; 503 when its only proof is
            a phone code and SMS is off.
    """
    if user_row.get("password_hash"):
        identifier = account_identifier(user_row) or user_row["id"]
        allowed, reason = await check_and_record_login(identifier, get_client_ip_info(request))
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please try again later.",
                headers={"Retry-After": "3600" if reason == "ip" else "900"},
            )
        if not password or not password_utils.verify_password(password, user_row["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")
        return

    if user_row.get("phone") and user_row.get("phone_verified"):
        if not is_sms_enabled():
            raise HTTPException(
                status_code=503,
                detail="Phone verification is currently unavailable. Please contact support.",
            )
        if not phone_code:
            raise HTTPException(
                status_code=400, detail="Enter the verification code sent to your phone."
            )
        # The stored number is canonical already; re-validating it against
        # today's send policy could refuse a number that policy once allowed.
        code = PhoneVerifyRequest.model_construct(phone=user_row["phone"], code=phone_code)
        await verify_phone_code(request, code, purpose, op_store, user_row["id"])
        return

    raise HTTPException(
        status_code=400,
        detail="This account has no password or verified phone to confirm this with. "
        "Please contact support.",
    )
