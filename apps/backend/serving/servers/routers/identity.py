"""Identity endpoints for cross-service single sign-on.

The gateway issues identity tokens that detached services — starting with the
cloud agent — verify without holding any secret of ours: OAuth 2.0 authorization
code with PKCE, and a public key published for verification.

Three endpoints, in the order they are used:

- ``GET /v1/identity/jwks`` — the verification key.
- ``POST /v1/identity/code`` — called by our own frontend on behalf of a
  signed-in user; returns a one-time code to hand back via the redirect.
- ``POST /v1/identity/token`` — called by the consuming service, unauthenticated
  but bound to the code by PKCE; returns the identity token.

Design notes live in :mod:`serving.utils.identity_tokens`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from serving.servers.deps import get_current_user, get_operational_store
from serving.utils.identity_keys import (
    IdentityKeyMisconfigured,
    IdentityKeyUnavailable,
    public_jwks,
)
from serving.utils.identity_tokens import (
    CODE_TTL_SECONDS,
    TOKEN_TTL_SECONDS,
    IdentityNotConfigured,
    InvalidAuthorizationRequest,
    expires_at,
    hash_code,
    mint_identity_token,
    new_code,
    validate_authorization_request,
    verify_pkce,
)

router = APIRouter(prefix="/v1/identity", tags=["identity"])

#: Consumers cache verification keys and re-fetch on an unknown ``kid``, so a
#: rotation is picked up by the miss rather than by expiry. Five minutes keeps
#: the endpoint from being hit on every token verification without making a
#: revocation wait long.
_CACHE_CONTROL = "public, max-age=300"


@router.get("/jwks")
async def jwks(response: Response) -> dict:
    """Publish the public half of the identity signing key.

    Args:
        response: The outgoing response, for cache headers.

    Returns:
        A JWKS document with a single RSA verification key.

    Raises:
        HTTPException: 404 if this deployment has no identity key configured;
            500 if one is configured but unusable.
    """
    try:
        document = public_jwks()
    except IdentityKeyUnavailable as exc:
        # Not a 500: a gateway with cross-service identity switched off is a
        # valid deployment. Consumers must be able to tell "not offered here"
        # from "offered but broken".
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "type": "identity_not_configured",
                    "message": "Cross-service identity is not configured on this deployment.",
                }
            },
        ) from exc
    except IdentityKeyMisconfigured as exc:
        # The other half of that distinction. A key is configured and cannot be
        # used, which is an operator error — answering 404 here would let a
        # production deploy with a mangled PEM pass for a feature nobody enabled.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": {
                    "type": "identity_key_misconfigured",
                    "message": str(exc),
                }
            },
        ) from exc

    response.headers["Cache-Control"] = _CACHE_CONTROL
    return document


def _error(status_code: int, error_type: str, message: str) -> HTTPException:
    """Build an error in the gateway's standard envelope."""
    return HTTPException(
        status_code=status_code,
        detail={"error": {"type": error_type, "message": message}},
    )


def _require_store(store: Any) -> Any:
    """Return the operational store, or refuse the request.

    Raises:
        HTTPException: 404 if this deployment has no user database, in which
            case there are no identities to federate in the first place.
    """
    if store is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "identity_not_configured",
            "Cross-service identity is not configured on this deployment.",
        )
    return store


class AuthorizationCodeRequest(BaseModel):
    """A signed-in user asking for a code to hand to a detached service."""

    client_id: str = Field(max_length=128)
    redirect_uri: str = Field(max_length=2048)
    code_challenge: str = Field(min_length=43, max_length=128)
    code_challenge_method: str = Field(default="S256", max_length=16)


class TokenExchangeRequest(BaseModel):
    """A detached service redeeming a code for an identity token."""

    code: str = Field(max_length=512)
    code_verifier: str = Field(min_length=43, max_length=128)
    client_id: str = Field(max_length=128)
    redirect_uri: str = Field(max_length=2048)


@router.post("/code")
async def create_authorization_code(
    body: AuthorizationCodeRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Issue a one-time authorization code for the signed-in user.

    Args:
        body: The client, redirect, and PKCE challenge.
        current_user: Resolved from the caller's gateway session token.
        store: Operational store.

    Returns:
        The code and its lifetime in seconds.

    Raises:
        HTTPException: 400 for an unacceptable request, 404 if identity is not
            configured here.
    """
    store = _require_store(store)
    try:
        validate_authorization_request(
            client_id=body.client_id,
            redirect_uri=body.redirect_uri,
            method=body.code_challenge_method,
        )
    except InvalidAuthorizationRequest as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "invalid_request", str(exc)) from exc
    except IdentityNotConfigured as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "identity_not_configured", str(exc)) from exc

    code, code_hash = new_code()
    await store.create_identity_auth_code(
        code_hash=code_hash,
        user_id=current_user["user_id"],
        client_id=body.client_id,
        redirect_uri=body.redirect_uri,
        code_challenge=body.code_challenge,
        expires_at=expires_at(seconds=CODE_TTL_SECONDS),
    )
    return {"code": code, "expires_in": CODE_TTL_SECONDS}


@router.post("/token")
async def exchange_authorization_code(
    body: TokenExchangeRequest,
    store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Exchange a one-time code for an identity token.

    Args:
        body: The code, its PKCE verifier, and the client/redirect it was
            issued for.
        store: Operational store.

    Returns:
        The identity token and its lifetime.

    Raises:
        HTTPException: 400 if the code is unusable, 403 if the account may no
            longer sign in, 404 if identity is not configured here.
    """
    store = _require_store(store)

    # Claimed before anything else is checked, and deliberately so. A failed
    # exchange must not leave the code usable for a second attempt — that is
    # what turns an intercepted code into a working one. The legitimate client
    # simply restarts the flow.
    claim = await store.consume_identity_auth_code(hash_code(body.code))
    invalid = _error(
        status.HTTP_400_BAD_REQUEST,
        "invalid_grant",
        "The authorization code is invalid, expired, or already used.",
    )
    if claim is None:
        raise invalid

    # Same error for every mismatch below: telling a caller which part of its
    # guess was right is an oracle.
    if body.client_id != claim["client_id"] or body.redirect_uri != claim["redirect_uri"]:
        raise invalid
    if not verify_pkce(verifier=body.code_verifier, challenge=claim["code_challenge"]):
        raise invalid

    # Re-read the account at exchange time. The code was issued up to a minute
    # ago, and a suspension in between must take effect immediately rather than
    # after the identity token expires downstream.
    user = await store.get_user_by_id(claim["user_id"])
    if user is None or user.get("status") != "active":
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "account_unavailable",
            "This account may not sign in.",
        )

    try:
        token = mint_identity_token(
            user_id=user["id"],
            email=user.get("email"),
            role=user.get("role") or "free",
        )
    except (IdentityNotConfigured, IdentityKeyUnavailable) as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "identity_not_configured", str(exc)) from exc

    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
    }
