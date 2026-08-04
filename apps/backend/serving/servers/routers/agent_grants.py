"""Internal endpoints the cloud agent's control plane calls to get capability.

Not public API. These sit behind a shared dispatch token and exist because the
control plane, after the split, cannot answer "may this user call this model"
on its own — it has no user table and no model registry. It asks; this gateway
decides. The reasoning behind each clamp lives in :mod:`serving.grants`.

Everything here is scope, never spend: a grant says *what* an attempt may call,
and the gateway's existing per-user quota continues to govern *how much*.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from serving import grants
from serving.agent_jobs.mcp_registry import McpRegistryError, get_registry
from serving.agent_jobs.visible_models import agent_visible_models
from serving.model_access import get_disabled_models_from_preferences
from serving.servers.deps import (
    get_log_store,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/agent-grants", tags=["internal"])

ENV_DISPATCH_TOKEN = "GATEWAY_GRANT_DISPATCH_TOKEN"

#: A status outside "active" is refused rather than defaulted.
_ACTIVE_STATUS = "active"

#: A renewal buys the same bounded step a mint does.
MAX_RENEW_TTL_S = grants.DEFAULT_GRANT_TTL_S


def require_dispatch_token(authorization: str | None = Header(None)) -> None:
    """Authorize an internal caller by the shared dispatch token.

    A deployment that has not set the token offers no internal endpoints at
    all — 404 rather than 401, because "this gateway does not federate
    capability" and "you got the password wrong" are different facts and an
    unconfigured deployment should not look like a guarded one.

    Raises:
        HTTPException: 404 when unconfigured, 401 when the token is absent or
            wrong.
    """
    expected = (os.environ.get(ENV_DISPATCH_TOKEN) or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "type": "internal_api_not_configured",
                    "message": "This deployment does not expose internal capability endpoints.",
                }
            },
        )
    presented = ""
    if authorization and authorization.startswith("Bearer "):
        presented = authorization[7:]
    # Constant-time: this compares a shared secret, and a timing oracle on it
    # is worth more to an attacker than on a per-user credential.
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"type": "unauthorized", "message": "Invalid dispatch token."}},
        )


def _error(status_code: int, error_type: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"type": error_type, "message": message}},
    )


def _require_store(store: Any) -> Any:
    if store is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "internal_api_not_configured",
            "This deployment has no user database, so it cannot mint grants.",
        )
    return store


class MintGrantRequest(BaseModel):
    """One attempt's requested capability."""

    user_id: str = Field(max_length=128)
    external_job_id: str = Field(max_length=128)
    external_attempt_id: str = Field(max_length=128)
    #: ``None`` means "everything this user's role can reach".
    allowed_models: list[str] | None = None
    #: ``None`` means the deployment's default MCP servers.
    allowed_mcp: list[str] | None = None
    ttl_seconds: int | None = None


def _grant_response(row: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
    """Shape a grant row for the control plane.

    Returns the **effective** scope and lifetime, not what was asked for: a
    caller that requested more must be able to see what it actually got, or it
    will build a job around capability it does not have.
    """
    body: dict[str, Any] = {
        "grant_id": row["grant_id"],
        "user_id": row["user_id"],
        "external_job_id": row["external_job_id"],
        "external_attempt_id": row["external_attempt_id"],
        "allowed_models": row["allowed_models"],
        "allowed_mcp": row["allowed_mcp"],
        "expires_at": row["expires_at"].isoformat(),
        "revoked_at": row["revoked_at"].isoformat() if row.get("revoked_at") else None,
    }
    if token is not None:
        body["token"] = token
    return body


async def _active_user(store: Any, user_id: str) -> dict[str, Any]:
    """Load the grant's subject, refusing anyone who may not use the platform.

    Raises:
        HTTPException: 403 for unknown or non-active accounts.
    """
    user = await store.get_user_by_id(user_id)
    if user is None or user.get("status") != _ACTIVE_STATUS:
        # One message for both cases: whether a given user id exists is not
        # something an internal caller needs, and saying so enumerates accounts.
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "subject_unavailable",
            "That user cannot be granted capability.",
        )
    return user


@router.post("")
async def mint_grant(
    body: MintGrantRequest,
    _: None = Depends(require_dispatch_token),
    store=Depends(get_operational_store),
    router_exec=Depends(get_router),
    visibility_resolver: Any = Depends(get_model_visibility_resolver),
) -> dict[str, Any]:
    """Mint (or return) the grant for one attempt.

    Args:
        body: The requested subject, scope and lifetime.
        _: Dispatch-token authorization.
        store: Operational store.
        router_exec: Model registry, for the role clamp.
        visibility_resolver: Runtime visibility overrides, so the clamp agrees
            with what the inference path would allow.

    Returns:
        The effective grant, with its bearer token.

    Raises:
        HTTPException: 400 for an unknown MCP server, 403 for an unusable
            subject, 404 when internal endpoints are not configured.
    """
    store = _require_store(store)
    user = await _active_user(store, body.user_id)

    # Models are clamped: a narrower list still runs. The user context mirrors
    # what the inference path builds, so the answer here and there agree.
    visible = await agent_visible_models(
        router_exec,
        visibility_resolver=visibility_resolver,
        user_ctx={
            "role": user.get("role") or "free",
            "user_id": user["id"],
            # Without this the resolver's denylist check reads an absent
            # key and passes, so a model the owner disabled resolves here
            # as visible — and a grant gets minted for it.
            "disabled_models": get_disabled_models_from_preferences(user.get("preferences")),
        },
    )
    effective_models = grants.clamp_models(body.allowed_models, visible=visible)

    # MCP is validated, not clamped. Dropping an unknown name would hand back a
    # grant that looks fine and produce an agent missing the one tool the task
    # was written around — a failure far from its cause.
    try:
        effective_mcp = get_registry().resolve(body.allowed_mcp)
    except McpRegistryError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "unknown_mcp_server", str(exc)) from exc

    ttl = grants.clamp_ttl(body.ttl_seconds)
    row = await store.upsert_agent_grant(
        grant_id=grants.new_grant_id(),
        user_id=user["id"],
        external_job_id=body.external_job_id,
        external_attempt_id=body.external_attempt_id,
        allowed_models=effective_models,
        allowed_mcp=list(effective_mcp),
        expires_at=grants.expiry_from(ttl),
    )

    # The row may be one that already existed: `(external_job_id,
    # external_attempt_id)` is unique, so a retried mint returns the first
    # grant rather than making a second. That is the behaviour we want, but it
    # is only *idempotent* when the request was the same request.
    #
    # Returning the old grant for a different subject or a different scope
    # would be a privilege decision made by whichever call happened to arrive
    # first — a caller asking for a narrower scope would silently be handed the
    # wider one, and a caller naming a different user would be handed a
    # capability belonging to someone else. The key is the control plane's own
    # id pair, so a collision here means its state and ours disagree; refusing
    # is the only answer that does not resolve that disagreement by guessing.
    if row["user_id"] != user["id"] or list(row["allowed_models"]) != list(effective_models):
        raise _error(
            status.HTTP_409_CONFLICT,
            "grant_conflict",
            "A grant already exists for this attempt with a different subject or scope.",
        )

    # The token is minted from what the store returned rather than from the id
    # generated above, which is not the id that won on a retry.
    return _grant_response(row, token=grants.mint_grant_token(row["grant_id"]))


@router.post("/{grant_id}/renew")
async def renew_grant(
    grant_id: str,
    _: None = Depends(require_dispatch_token),
    store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Extend a live grant by another bounded step.

    Deliberately consults no attempt or lease state: after the split this
    gateway holds none. The control plane stops renewing when its own store
    says the attempt was superseded, and the grant then dies of its TTL whether
    or not a revoke call ever succeeds.

    Args:
        grant_id: The grant to extend.
        _: Dispatch-token authorization.
        store: Operational store.

    Returns:
        The renewed grant, without a token — the caller already has it.

    Raises:
        HTTPException: 403 if the subject is no longer active, 404 if the grant
            is unknown, revoked, or already expired.
    """
    store = _require_store(store)
    existing = await store.get_agent_grant(grant_id)
    if existing is None:
        raise _error(status.HTTP_404_NOT_FOUND, "unknown_grant", "No such grant.")

    # Re-checked on every renewal: a user suspended mid-job must stop being
    # able to spend, and renewal is the only recurring moment to notice.
    await _active_user(store, existing["user_id"])

    row = await store.renew_agent_grant(grant_id, expires_at=grants.expiry_from(MAX_RENEW_TTL_S))
    if row is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "grant_not_live",
            "That grant is revoked or expired and cannot be renewed.",
        )
    return _grant_response(row)


@router.get("/{grant_id}/usage")
async def grant_usage(
    grant_id: str,
    _: None = Depends(require_dispatch_token),
    store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Report what the grant's job has spent so far.

    **Informational attribution, not a limit.** Nothing consults this to decide
    whether a call may proceed — that is the account's daily quota, enforced in
    ``model_auth``. This exists so the control plane's UI can show an owner
    what a job cost, and reading it as a ceiling would reintroduce the per-job
    budget the design removed.

    Numbers come from the billing ledger, keyed by the grant's
    ``external_job_id``, so they reflect what the gateway actually billed
    rather than anything an agent reports about itself.

    Args:
        grant_id: The grant whose job to report on.
        _: Dispatch-token authorization.
        store: Operational store.
        log_store: Billing ledger.

    Returns:
        Spend, call count, and token totals for the grant's job.

    Raises:
        HTTPException: 404 for an unknown grant, 503 without a ledger.
    """
    store = _require_store(store)
    row = await store.get_agent_grant(grant_id)
    if row is None:
        raise _error(status.HTTP_404_NOT_FOUND, "unknown_grant", "No such grant.")
    if log_store is None:
        # An empty report would read as "this job spent nothing", which is a
        # different and much more reassuring claim than "we cannot tell".
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ledger_unavailable",
            "Usage cannot be reported right now.",
        )

    job_id = row["external_job_id"]
    usage = await log_store.get_agent_job_usage(job_id)
    return {
        "grant_id": grant_id,
        "external_job_id": job_id,
        "spent_usd": float(await log_store.get_agent_job_cost(job_id)),
        "request_count": int(usage.get("calls", 0)),
        "tokens_in": int(usage.get("tokens_in", 0)),
        "tokens_out": int(usage.get("tokens_out", 0)),
    }


@router.post("/{grant_id}/revoke")
async def revoke_grant(
    grant_id: str,
    _: None = Depends(require_dispatch_token),
    store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Withdraw a grant now, rather than waiting for its TTL.

    Args:
        grant_id: The grant to revoke.
        _: Dispatch-token authorization.
        store: Operational store.

    Returns:
        Whether this call performed the revocation.

    Raises:
        HTTPException: 404 if the grant is unknown.
    """
    store = _require_store(store)
    if await store.get_agent_grant(grant_id) is None:
        raise _error(status.HTTP_404_NOT_FOUND, "unknown_grant", "No such grant.")
    revoked = await store.revoke_agent_grant(grant_id)
    return {"grant_id": grant_id, "revoked": True, "already_revoked": not revoked}
