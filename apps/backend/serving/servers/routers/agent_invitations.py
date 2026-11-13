"""Agent invitations: an approved agent user sets a password and lands here.

A detached agent service and this gateway keep separate account systems. When
the agent approves a user, it asks this gateway (over the dispatch token) to
mint a one-time invitation token bound to that user's verified email, and
emails them a link. The link lands on this deployment's redemption page,
where the user sets a password *here* — no password or hash ever crosses
between the services.

Redemption is one store transaction: claim the token, find or create the
account for the invited email, ensure its organization, and issue exactly
one ``agent``-scoped API key (see ``serving.model_access``). The browser is
then signed in with the same session a password login issues, and the
response carries the new API key once, for the page to hand on to the agent
service as a model connection.

An invitation never lifts a sanction: a suspended, rejected or deleting
account refuses redemption and the token stays unconsumed. Setting the
password always happens — for an account that already existed too — and it
revokes that account's earlier sessions, as a password reset does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from serving.auth.browser_sessions import prepare_sign_in
from serving.exceptions import AgentInviteSubjectUnavailableError, APIKeyLimitReachedError
from serving.model_access import AGENT_CREDENTIAL_SCOPE
from serving.schemas_auth import AgentInviteRedeemResponse
from serving.servers.auth import generate_api_key, hash_api_key, log_admin_action
from serving.servers.deps import get_db_logger, get_operational_store
from serving.servers.routers.auth_routes import start_user_session
from serving.servers.routers.internal_auth import (
    error as _error,
    require_dispatch_token,
    require_store as _require_store,
)
from serving.servers.routers.user_routes import (
    MAX_ACTIVE_API_KEYS_PER_ACCOUNT,
    get_default_daily_quota_for_role,
)
from serving.setup_state import refresh_setup_state
from serving.utils import password as password_utils
from serving.utils.identity_tokens import hash_code, new_code
from serving.utils.jwt import generate_ulid
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip, get_client_ip_info
from serving.utils.signup_rate_limit import check_and_record_signup

logger = get_logger(__name__)

#: How long an invitation link stays redeemable. Seven days is long enough
#: for an approval email to be found and acted on, short enough that a
#: forwarded link is not a standing credential.
INVITE_TTL_SECONDS = 7 * 24 * 60 * 60

router = APIRouter(prefix="/v1/agent-invites", tags=["agent-invitations"])

#: Guarded at the router, like the other internal surfaces: every route in
#: this half of the module needs the dispatch token.
internal_router = APIRouter(
    prefix="/internal/agent-invitations",
    tags=["internal"],
    dependencies=[Depends(require_dispatch_token)],
)


class RedeemRequest(BaseModel):
    """Set a password against an invitation token."""

    token: str = Field(max_length=512)
    password: str = Field(min_length=8, max_length=1024)
    user_name: str | None = Field(default=None, max_length=255)
    accepted_tos: bool


class MintRequest(BaseModel):
    """Ask for an invitation token for one approved agent account."""

    agent_account_id: str = Field(max_length=255)
    email: str = Field(max_length=320)


class RevokeRequest(BaseModel):
    """Revoke the scoped key an agent account redeemed."""

    agent_account_id: str = Field(max_length=255)


@router.post("/redeem", response_model=AgentInviteRedeemResponse)
async def redeem_agent_invite(
    request: Request,
    response: Response,
    body: RedeemRequest,
    op_store=Depends(get_operational_store),
    db_logger=Depends(get_db_logger),
) -> AgentInviteRedeemResponse:
    """Redeem an invitation: set a password, open the account, sign in.

    The token names the account: only the email it was issued for can be
    created or updated, so a forwarded link cannot register a different
    address. Every refusal before the store call (terms, password strength,
    rate limit) leaves the token usable; the store's own refusals roll its
    transaction back with the same effect.

    Raises:
        HTTPException: 400 for unaccepted terms or a weak password, 403 when
            the invited email's account may not sign in, 409 at the key
            ceiling, 410 for a used, superseded or expired token, 429 when
            rate-limited, 503 when this deployment has no user database.
    """
    # Redemption can create the first account on a fresh deployment, so it
    # waits for first-run setup like every other registration route: a
    # stranger holding no invitation must not be able to complete setup
    # with an account of their own. An invitation token already names an
    # approved agent account, but the gate is the same one, for the same
    # reason.
    if await refresh_setup_state():
        raise HTTPException(status_code=503, detail="This deployment has not been set up yet.")
    store = _require_store(op_store)
    # Creating an account is signup-shaped work and shares its per-address
    # budget, so redemption cannot be amplified into account creation.
    allowed, _reason = await check_and_record_signup(get_client_ip_info(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please try again later.",
            headers={"Retry-After": "3600"},
        )
    if not body.accepted_tos:
        raise HTTPException(
            status_code=400,
            detail="You must agree to the Terms of Service to create an account",
        )
    is_valid, error_msg = password_utils.validate_password_strength(body.password)
    if not is_valid:
        raise HTTPException(
            status_code=400, detail=error_msg or "Password does not meet security requirements"
        )

    # Refuse before spending Argon2 work a flood cannot force us to queue.
    password_utils.ensure_password_capacity()
    password_hash = await password_utils.hash_password_async(body.password)
    api_key = generate_api_key()
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        runtime_settings = get_runtime_settings_instance()
    except RuntimeError:
        runtime_settings = None
    quota = await get_default_daily_quota_for_role("free", runtime_settings)

    try:
        row = await store.redeem_agent_invite(
            token_hash=hash_code(body.token),
            user_id=generate_ulid(),
            user_name=(body.user_name or "").strip() or None,
            password_hash=password_hash,
            api_key_hash=hash_api_key(api_key),
            api_key_prefix=api_key[:12],
            credential_scope=AGENT_CREDENTIAL_SCOPE,
            quota_daily_cost_usd=quota,
            max_active_keys=MAX_ACTIVE_API_KEYS_PER_ACCOUNT,
        )
    except AgentInviteSubjectUnavailableError as exc:
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "subject_unavailable",
            "This email's account may not sign in. The invitation was not used.",
        ) from exc
    except APIKeyLimitReachedError as exc:
        raise _error(
            status.HTTP_409_CONFLICT,
            "api_key_limit_reached",
            "This account already holds as many API keys as it may. "
            "Revoke one, then use the invitation link again.",
        ) from exc
    if row is None:
        raise _error(
            status.HTTP_410_GONE,
            "invite_unusable",
            "This invitation link is invalid, already used, or expired. Ask for a new invitation.",
        )

    user_row = await store.get_user_by_id(row["user_id"])
    browser = await prepare_sign_in(store, request, row["user_id"])
    await store.update_user_last_login(row["user_id"])
    login = await start_user_session(
        store,
        response,
        browser,
        user_row,
        user_role=user_row.get("role") or "free",
        request=request,
    )
    try:
        await store.record_login_event(
            email=row["email"],
            outcome="success",
            failure_reason=None,
            user_id=row["user_id"],
            ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.exception("agent_invite_login_event_write_failed")
    await log_admin_action(
        db_logger,
        action="agent_invitation_redeemed",
        target_user_id=row["user_id"],
        details={"account_created": row["created"], "credential_scope": AGENT_CREDENTIAL_SCOPE},
    )
    logger.info("agent_invitation_redeemed user_id=%s created=%s", row["user_id"], row["created"])
    return AgentInviteRedeemResponse(
        **login.model_dump(), api_key=api_key, account_created=bool(row["created"])
    )


@internal_router.post("", status_code=status.HTTP_201_CREATED)
async def mint_agent_invitation(
    body: MintRequest,
    response: Response,
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Mint a one-time invitation token for an approved agent account.

    The caller emails the token in a link to this deployment's redemption
    page; only its hash is stored. Minting supersedes every still-usable
    earlier token for the account, so only the latest link works. Minting is
    refused once the account has redeemed and not been revoked since: a
    retry that lost the "already done" answer must not issue a fresh link
    that resets a live account's password. After a revocation it mints
    again, which is how a re-approved account is re-invited.
    """
    store = _require_store(op_store)
    link = await store.get_agent_provisioned_account(body.agent_account_id)
    if link is not None and link.get("revoked_at") is None:
        raise _error(
            status.HTTP_409_CONFLICT,
            "already_provisioned",
            "This agent account has already redeemed an invitation.",
        )
    token, token_hash = new_code()
    await store.create_agent_invite_token(
        token_hash=token_hash,
        agent_account_id=body.agent_account_id,
        email=body.email.strip().lower(),
        expires_at=datetime.now(UTC) + timedelta(seconds=INVITE_TTL_SECONDS),
    )
    response.headers["Cache-Control"] = "no-store"
    return {"token": token, "expires_in": INVITE_TTL_SECONDS}


@internal_router.get("/{agent_account_id}")
async def agent_invitation_status(
    agent_account_id: str,
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Whether an agent account has a live provisioned account here.

    Lets the inviting service poll an invitation it emailed to completion
    without minting again (a re-mint answers 409 while the link is live, and
    supersedes it otherwise — neither is a status read).
    """
    store = _require_store(op_store)
    link = await store.get_agent_provisioned_account(agent_account_id)
    provisioned = link is not None and link.get("revoked_at") is None
    return {"provisioned": provisioned, "revoked": link is not None and not provisioned}


@internal_router.post("/revoke")
async def revoke_agent_invitation(
    body: RevokeRequest,
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Revoke the scoped key an agent account redeemed, if it has one.

    Idempotent: an account that never redeemed answers ``revoked: false``
    rather than an error, so a caller retiring access need not first ask
    whether redemption ever happened.
    """
    store = _require_store(op_store)
    row = await store.revoke_agent_provisioned_account(body.agent_account_id)
    return {"revoked": row is not None}
