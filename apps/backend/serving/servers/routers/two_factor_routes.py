"""An account's second sign-in factor: adding an authenticator app, and giving its codes.

These are the signed-in account's own routes, reached with its session token
like the rest of ``/auth`` and ``/user``. They are deliberately not behind the
administrator check: an administrator who owes a code, or has no authenticator
yet, is refused by every ``/admin`` route (``serving.servers.deps``) and comes
here to give one.

What each step asks for:

- **Adding the first authenticator** asks for the account's password. A copied
  session must not be able to add a device of its own to somebody's account.
- **Replacing it**, **removing it** and **new recovery codes** ask for a code
  given within the last few minutes: only the holder of the current device, or
  of a recovery code, changes what the second factor is.
- **Giving a code** asks for nothing else. Wrong codes in a row hold the
  account's codes off, for longer each time, so it is never a place to guess
  six digits; a recovery code, which cannot be guessed, still works then.

The arithmetic and the policy are in ``serving.auth.two_factor``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.audit import AuditTarget
from serving.auth.two_factor import (
    LOCK_EVERY_ATTEMPTS,
    LOCK_SECONDS,
    MAX_LOCK_SECONDS,
    admin_two_factor_required,
    decrypt_secret,
    encrypt_secret,
    generate_recovery_codes,
    generate_secret,
    has_authenticator,
    hash_recovery_code,
    looks_like_totp,
    matching_step,
    normalize_code,
    provisioning_uri,
    read_session_two_factor,
    request_environment,
    require_session_two_factor,
    step_up_window,
)
from serving.config.site_identity import get_site_identity
from serving.schemas_auth import (
    TwoFactorCodeRequest,
    TwoFactorRecoveryCodesResponse,
    TwoFactorSetupRequest,
    TwoFactorSetupResponse,
    TwoFactorStatusResponse,
    TwoFactorVerifyResponse,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_current_user, get_operational_store, user_audit_actor
from serving.servers.routers.reauth import account_identifier, confirm_account_owner
from serving.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/auth/two-factor", tags=["Authentication"])

_NOT_SET_UP = "Two-factor authentication is not set up for this account."
_WRONG_CODE = "That code is not valid. Enter the current code from your authenticator app."


def _store(op_store: Any) -> Any:
    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available")
    return op_store


async def _account(op_store: Any, current_user: dict[str, Any]) -> dict[str, Any]:
    user_row = await op_store.get_user_by_id(current_user["user_id"])
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    return user_row


def _held_off(until: datetime) -> HTTPException:
    wait = max(1, math.ceil((until - datetime.now(UTC)).total_seconds()))
    return HTTPException(
        status_code=429,
        detail="Too many wrong codes. Try again later, or use a recovery code.",
        headers={"Retry-After": str(wait)},
    )


async def _begin_attempt(op_store: Any, user_id: str) -> datetime | None:
    return await op_store.begin_two_factor_attempt(
        user_id,
        lock_every=LOCK_EVERY_ATTEMPTS,
        lock_seconds=LOCK_SECONDS,
        max_lock_seconds=MAX_LOCK_SECONDS,
    )


async def _spend_code(op_store: Any, user_id: str, row: dict[str, Any], raw: str) -> str | None:
    """Spend *raw* against the account's authenticator; return how it matched, or None.

    ``"totp"`` for a code from the app, ``"recovery_code"`` for one of the
    one-time codes. Either is spent by the statement that accepts it, so the
    same code given twice at once is good once. Every attempt is counted
    before it is looked at; a right code ends the count.

    Raises:
        HTTPException: 429 while too many wrong codes in a row hold the
            account's codes off. A right recovery code is accepted even then:
            it cannot be guessed, and it is how the owner gets past a hold
            that somebody else's guesses caused.
    """
    code = normalize_code(raw)
    if not looks_like_totp(code):
        if await op_store.use_recovery_code(user_id, hash_recovery_code(code)):
            return "recovery_code"
        held_until = await _begin_attempt(op_store, user_id)
        if held_until is not None:
            raise _held_off(held_until)
        return None

    held_until = await _begin_attempt(op_store, user_id)
    if held_until is not None:
        raise _held_off(held_until)
    secret = decrypt_secret(row["secret_encrypted"] or "")
    if secret is None:
        logger.error("two_factor: stored secret of %s cannot be decrypted", user_id)
        return None
    step = matching_step(secret, code, after_step=row["last_used_step"])
    if step is not None and await op_store.use_two_factor_step(user_id, step):
        return "totp"
    return None


async def _audit(
    request: Request,
    op_store: Any,
    current_user: dict[str, Any],
    action: str,
    *,
    success: bool = True,
    **details: Any,
) -> None:
    await log_admin_action(
        op_store,
        action=action,
        details={
            "sid": current_user.get("sid"),
            "environment": request_environment(request),
            **details,
        },
        success=success,
        actor=user_audit_actor(request, current_user),
        target=AuditTarget.user(current_user["user_id"]),
    )


@router.get("", response_model=TwoFactorStatusResponse)
async def two_factor_status(
    request: Request,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> TwoFactorStatusResponse:
    """Say whether the account has an authenticator and whether this session owes a code."""
    op_store = _store(op_store)
    user_id = current_user["user_id"]
    row = await op_store.get_two_factor(user_id)
    enabled = has_authenticator(row)
    state = await read_session_two_factor(op_store, user_id, current_user.get("sid"))
    return TwoFactorStatusResponse(
        enabled=enabled,
        pending=bool(row and row.get("pending_secret_encrypted")),
        required=bool(current_user["is_admin"]) and admin_two_factor_required(),
        session_verified=state.refusal(request_environment(request)) is None,
        recovery_codes_left=int(row["recovery_codes_left"]) if enabled else 0,
    )


@router.post("/setup", response_model=TwoFactorSetupResponse)
async def start_two_factor_setup(
    request: Request,
    body: TwoFactorSetupRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> TwoFactorSetupResponse:
    """Show a new secret for an authenticator app; it counts once ``/confirm`` gets a code.

    A first authenticator needs the account's password (400 when wrong or
    missing). Replacing one needs a recent code from the current one (403
    ``TWO_FACTOR_REQUIRED``), which keeps working until the new one is
    confirmed.
    """
    op_store = _store(op_store)
    user_id = current_user["user_id"]
    user_row = await _account(op_store, current_user)
    row = await op_store.get_two_factor(user_id)

    if has_authenticator(row):
        await require_session_two_factor(
            request,
            op_store,
            user_id,
            current_user.get("sid"),
            within=step_up_window(),
            action="Replacing your authenticator",
        )
    else:
        if not user_row.get("password_hash"):
            raise HTTPException(
                status_code=400,
                detail="Adding an authenticator needs your password, and this account has "
                "none. Set one in Settings first.",
            )
        await confirm_account_owner(
            request,
            op_store,
            user_row,
            password=body.password,
            phone_code=None,
            purpose="set_password",
        )

    secret = generate_secret()
    await op_store.set_pending_two_factor(user_id, encrypt_secret(secret))
    return TwoFactorSetupResponse(
        secret=secret,
        otpauth_uri=provisioning_uri(
            secret,
            account=account_identifier(user_row) or user_id,
            issuer=get_site_identity().name,
        ),
    )


@router.post("/confirm", response_model=TwoFactorRecoveryCodesResponse)
async def confirm_two_factor_setup(
    request: Request,
    body: TwoFactorCodeRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> TwoFactorRecoveryCodesResponse:
    """Give the first code from the new authenticator, and receive the recovery codes.

    From here the account's codes come from this device; an earlier device's
    stop, and so does what other sessions proved with it. This session counts
    as verified.
    """
    op_store = _store(op_store)
    user_id = current_user["user_id"]
    row = await op_store.get_two_factor(user_id)
    pending = row.get("pending_secret_encrypted") if row else None
    if not pending:
        raise HTTPException(
            status_code=409, detail="There is no authenticator waiting to be confirmed."
        )

    # Nothing to guess here: whoever started this setup was shown the secret.
    secret = decrypt_secret(pending)
    step = matching_step(secret, normalize_code(body.code)) if secret else None
    if step is None:
        raise HTTPException(status_code=400, detail=_WRONG_CODE)

    recovery_codes = generate_recovery_codes()
    confirmed = await op_store.confirm_two_factor(
        user_id,
        pending_secret_encrypted=pending,
        step=step,
        recovery_code_hashes=[hash_recovery_code(code) for code in recovery_codes],
    )
    if not confirmed:
        # Another request confirmed it, or started setup again, in between.
        raise HTTPException(
            status_code=409, detail="This setup was replaced by a newer one. Start again."
        )
    await op_store.mark_session_two_factor(
        user_id, current_user.get("sid") or "", request_environment(request)
    )
    await _audit(
        request, op_store, current_user, "two_factor.enable", replaced=has_authenticator(row)
    )
    return TwoFactorRecoveryCodesResponse(recovery_codes=recovery_codes)


@router.post("/verify", response_model=TwoFactorVerifyResponse)
async def verify_two_factor(
    request: Request,
    body: TwoFactorCodeRequest,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> TwoFactorVerifyResponse:
    """Give a code, so this session counts as verified from the client giving it.

    Takes a code from the authenticator app, or a recovery code, which is
    spent. 400 for a wrong one, 409 for an account with no authenticator, 429
    while too many wrong ones in a row hold the account's codes off.
    """
    op_store = _store(op_store)
    user_id = current_user["user_id"]
    row = await op_store.get_two_factor(user_id)
    if not has_authenticator(row):
        raise HTTPException(status_code=409, detail=_NOT_SET_UP)

    method = await _spend_code(op_store, user_id, row, body.code)
    if method is None:
        await _audit(request, op_store, current_user, "two_factor.verify", success=False)
        raise HTTPException(status_code=400, detail=_WRONG_CODE)

    marked = await op_store.mark_session_two_factor(
        user_id, current_user.get("sid") or "", request_environment(request)
    )
    if not marked:
        # The session ended between the token check and here.
        raise HTTPException(status_code=401, detail="This session has ended. Please login again.")
    await _audit(request, op_store, current_user, "two_factor.verify", method=method)
    left = int(row["recovery_codes_left"]) - (1 if method == "recovery_code" else 0)
    return TwoFactorVerifyResponse(recovery_codes_left=max(left, 0))


@router.post("/recovery-codes", response_model=TwoFactorRecoveryCodesResponse)
async def new_recovery_codes(
    request: Request,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> TwoFactorRecoveryCodesResponse:
    """Replace the account's recovery codes; needs a recent code."""
    op_store = _store(op_store)
    user_id = current_user["user_id"]
    if not has_authenticator(await op_store.get_two_factor(user_id)):
        raise HTTPException(status_code=409, detail=_NOT_SET_UP)
    await require_session_two_factor(
        request,
        op_store,
        user_id,
        current_user.get("sid"),
        within=step_up_window(),
        action="New recovery codes",
    )
    recovery_codes = generate_recovery_codes()
    replaced = await op_store.replace_recovery_codes(
        user_id, [hash_recovery_code(code) for code in recovery_codes]
    )
    if not replaced:
        raise HTTPException(status_code=409, detail=_NOT_SET_UP)
    await _audit(request, op_store, current_user, "two_factor.recovery_codes")
    return TwoFactorRecoveryCodesResponse(recovery_codes=recovery_codes)


@router.delete("", response_model=TwoFactorStatusResponse)
async def remove_two_factor(
    request: Request,
    current_user=Depends(get_current_user),
    op_store=Depends(get_operational_store),
) -> TwoFactorStatusResponse:
    """Remove the account's authenticator and recovery codes; needs a recent code.

    409 for an administrator where the deployment requires a second factor of
    them: without one the account would have no administrator access left to
    add it back with. Replace the device through ``/setup`` instead.
    """
    op_store = _store(op_store)
    user_id = current_user["user_id"]
    if not has_authenticator(await op_store.get_two_factor(user_id)):
        raise HTTPException(status_code=409, detail=_NOT_SET_UP)
    required = bool(current_user["is_admin"]) and admin_two_factor_required()
    if required:
        raise HTTPException(
            status_code=409,
            detail="Administrators keep two-factor authentication on here. To change "
            "devices, add the new authenticator; it replaces this one.",
        )
    await require_session_two_factor(
        request,
        op_store,
        user_id,
        current_user.get("sid"),
        within=step_up_window(),
        action="Removing your authenticator",
    )
    await op_store.delete_two_factor(user_id)
    await _audit(request, op_store, current_user, "two_factor.disable")
    return TwoFactorStatusResponse(
        enabled=False, pending=False, required=False, session_verified=False, recovery_codes_left=0
    )
