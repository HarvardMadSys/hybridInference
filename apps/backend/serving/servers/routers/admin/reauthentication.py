"""The administrator, asked again before a change no session should make alone.

A bearer token proves a session, not a person: a stolen console token holds
administrator access for its lifetime, and ``ADMIN_TOKEN`` for good. What would
outlive that session, or hand out what the deployment keeps secret, asks the
signed-in administrator to prove it is still them first: exporting secret
values, restoring security settings from a backup, changing a security-relevant
setting, raising an account's role, minting an API key for another account,
changing who may use a model.

How they prove it depends on what the account has. One with an authenticator
app gives a code from it (``serving.auth.two_factor``): the session must have
given one within the last few minutes, from the kind of client making this
request, and the console asks for six digits when it has not. One without
gives its password, which is all there is to ask it for -- and which whoever
copied the session may have copied too, so an administrator account is better
off with an authenticator.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from serving.auth.two_factor import (
    read_session_two_factor,
    request_environment,
    step_up_window,
)
from serving.exceptions import TwoFactorRequiredError
from serving.servers.deps import get_admin_session_id, get_admin_user_id
from serving.servers.routers.reauth import confirm_account_owner


async def confirmed_by_two_factor(request: Request, op_store: Any, *, action: str) -> bool:
    """Whether the administrator behind this request has just given a code.

    False for a caller with no authenticator to ask: ``ADMIN_TOKEN``, and an
    account that has not added one. The caller decides what that means.

    Raises:
        TwoFactorRequiredError: 403 when the account has an authenticator and
            this session's code is missing, too old, or from another client.
    """
    user_id = get_admin_user_id(request)
    if user_id is None or op_store is None:
        return False
    state = await read_session_two_factor(op_store, user_id, get_admin_session_id(request))
    if not state.enrolled:
        return False
    reason = state.refusal(request_environment(request), within=step_up_window())
    if reason is not None:
        raise TwoFactorRequiredError(reason, action)
    return True


async def confirm_administrator(
    request: Request, op_store: Any, password: str | None, *, action: str
) -> None:
    """Raise unless the administrator behind this request proves it is still them.

    An account with an authenticator is confirmed by a recent code
    (:func:`confirmed_by_two_factor`) and *password* is not looked at. Any
    other account gives its password, checked by :func:`confirm_account_owner`,
    so every attempt counts against the sign-in limits. An ``ADMIN_TOKEN``
    caller has no account to confirm, and an account without a password (made
    by phone) is refused: confirming by phone code would need a new code
    purpose.

    Args:
        request: The admin request; ``verify_admin_access`` has run on it.
        op_store: The operational store, to read the account.
        password: What the administrator typed, if anything.
        action: What is being confirmed, as the start of a sentence
            ("Exporting secret values").

    Raises:
        HTTPException: 403 for the admin token; 400 for an account without a
            password or a wrong or missing one; 429 when rate-limited.
        TwoFactorRequiredError: 403 when the account has an authenticator and
            the session owes a code.
    """
    user_id = get_admin_user_id(request)
    if user_id is None:
        raise HTTPException(
            status_code=403,
            detail=f"{action} needs an administrator's password. Sign in to the console as an "
            "administrator: the admin token cannot confirm it.",
        )
    if await confirmed_by_two_factor(request, op_store, action=action):
        return
    user_row = await op_store.get_user_by_id(user_id) if op_store else None
    if not user_row:
        raise HTTPException(status_code=403, detail="Admin account not found.")
    if not user_row.get("password_hash"):
        raise HTTPException(
            status_code=400,
            detail=f"{action} needs your password, and this account has none. Set one in "
            "Settings first.",
        )
    await confirm_account_owner(
        request,
        op_store,
        user_row,
        password=password,
        phone_code=None,
        purpose="config_backup",
    )
