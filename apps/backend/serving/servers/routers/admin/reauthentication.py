"""The administrator's password, asked again before a change no session should make alone.

A bearer token proves a session, not a person: a stolen console token holds
administrator access for its lifetime, and ``ADMIN_TOKEN`` for good. What would
outlive that session, or hand out what the deployment keeps secret, asks for
the signed-in administrator's password first: exporting secret values,
restoring security settings from a backup, changing a security-relevant
setting, raising an account's role, minting an API key for another account.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from serving.servers.deps import get_admin_user_id
from serving.servers.routers.reauth import confirm_account_owner


async def confirm_administrator(
    request: Request, op_store: Any, password: str | None, *, action: str
) -> None:
    """Raise unless the administrator behind this request gives the account's password.

    The password is checked by :func:`confirm_account_owner`, as revealing a
    saved API key checks it, so every attempt counts against the sign-in
    limits. An ``ADMIN_TOKEN`` caller has no account to confirm, and an
    account without a password (made by phone) is refused: confirming by phone
    code would need a new code purpose.

    Args:
        request: The admin request; ``verify_admin_access`` has run on it.
        op_store: The operational store, to read the account.
        password: What the administrator typed, if anything.
        action: What is being confirmed, as the start of a sentence
            ("Exporting secret values").

    Raises:
        HTTPException: 403 for the admin token; 400 for an account without a
            password or a wrong or missing one; 429 when rate-limited.
    """
    user_id = get_admin_user_id(request)
    if user_id is None:
        raise HTTPException(
            status_code=403,
            detail=f"{action} needs an administrator's password. Sign in to the console as an "
            "administrator: the admin token cannot confirm it.",
        )
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
