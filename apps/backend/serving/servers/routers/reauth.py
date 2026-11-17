"""Confirm that a signed-in account's owner is present before a sensitive change.

A bearer token proves a session, not a person: a token lifted from a shared
computer or a script on the page holds it for its lifetime. Deleting the
account, changing its sign-in email and giving it a first password each ask
again, the way the account signs in -- its password, or a code sent to its
verified phone when it has none, or a fresh sign-in at its identity provider
when it has neither.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from serving.schemas_auth import PhoneVerifyRequest
from serving.servers.routers.phone_auth_routes import verify_phone_code
from serving.utils import password as password_utils
from serving.utils.device_cookie import recognized_device
from serving.utils.login_rate_limit import check_and_record_login
from serving.utils.request_ip import get_client_ip_info
from serving.utils.sms import is_sms_enabled

#: How long a fresh sign-in at an identity provider confirms an action for.
SSO_REAUTH_MAX_AGE_SECONDS = 300


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
    sid: str | None = None,
) -> None:
    """Raise unless *password* or *phone_code* proves the owner of *user_row*.

    An account with a password must give it; attempts count against the login
    rate limit, so this is never a second place to guess it. An account
    without one must give a code for *purpose* sent to its verified phone. An
    account with neither, which signs in through an identity provider, must
    have just signed in there again for *purpose* in the session *sid* names
    (the ``sid`` of the caller's access token).

    Raises:
        HTTPException: 400 for a wrong or missing proof, or an account with no
            way to give one; 429 when rate-limited; 503 when its only proof is
            a phone code and SMS is off.
    """
    if user_row.get("password_hash"):
        identifier = account_identifier(user_row) or user_row["id"]
        # The same limits as sign-in, including the device cookie's own budget:
        # someone else's guesses at this name must not block its owner here.
        # A signed-in session, so the breaker an anonymous flood of unresolved
        # sign-ins trips does not refuse it; its account windows still apply.
        allowed, reason = await check_and_record_login(
            identifier,
            get_client_ip_info(request),
            device=recognized_device(request, identifier),
            signed_in=True,
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please try again later.",
                headers={"Retry-After": "3600" if reason == "ip" else "900"},
            )
        # A signed-in session: an anonymous flood must not refuse its owner here.
        if not password or not await password_utils.verify_password_async(
            password, user_row["password_hash"], shed=False
        ):
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

    # Neither: an account that signs in through an identity provider confirms by
    # signing in there again (POST /auth/sso/reauth/start). That confirmation is
    # spent here, once, by the session and for the purpose it was made for.
    if sid and await op_store.consume_sso_reauth_proof(
        user_id=user_row["id"],
        sid=sid,
        purpose=purpose,
        max_age_seconds=SSO_REAUTH_MAX_AGE_SECONDS,
    ):
        return
    if await op_store.list_user_identities(user_row["id"]):
        raise HTTPException(
            status_code=400,
            detail="Confirm your identity with your organization's sign-in first.",
        )

    raise HTTPException(
        status_code=400,
        detail="This account has no password or verified phone to confirm this with. "
        "Please contact support.",
    )
