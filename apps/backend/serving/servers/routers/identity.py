"""Identity endpoints for cross-service single sign-on.

The gateway issues identity tokens that detached services — starting with the
cloud agent — verify without holding any secret of ours. This module publishes
the public key they verify against. Token issuance itself is added alongside it
in a later step; JWKS lands first so consumers have something to fetch while
that is built.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from serving.utils.identity_keys import (
    IdentityKeyMisconfigured,
    IdentityKeyUnavailable,
    public_jwks,
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
