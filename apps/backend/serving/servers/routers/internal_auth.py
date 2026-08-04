"""Shared authorization and error shapes for the internal API.

The endpoints the cloud agent's control plane calls are not public API. They
sit behind one shared dispatch token, and they answer in the gateway's standard
error envelope. Both live here rather than in whichever router happened to be
written first, so a second consumer does not import another module's private
names and freeze them in place.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import Header, HTTPException, status

ENV_DISPATCH_TOKEN = "GATEWAY_GRANT_DISPATCH_TOKEN"


def error(status_code: int, error_type: str, message: str) -> HTTPException:
    """Build an error in the gateway's standard envelope."""
    return HTTPException(
        status_code=status_code,
        detail={"error": {"type": error_type, "message": message}},
    )


def require_dispatch_token(authorization: str | None = Header(None)) -> None:
    """Authorize an internal caller by the shared dispatch token.

    A deployment that has not set the token offers no internal endpoints at
    all — 404 rather than 401, because "this gateway does not federate
    capability" and "you got the password wrong" are different facts, and an
    unconfigured deployment should not look like a guarded one.

    Raises:
        HTTPException: 404 when unconfigured, 401 when the token is absent or
            wrong.
    """
    expected = (os.environ.get(ENV_DISPATCH_TOKEN) or "").strip()
    if not expected:
        raise error(
            status.HTTP_404_NOT_FOUND,
            "internal_api_not_configured",
            "This deployment does not expose internal capability endpoints.",
        )
    presented = ""
    if authorization and authorization.startswith("Bearer "):
        presented = authorization[7:]
    # Constant-time: this compares a shared secret, and a timing oracle on it
    # is worth more to an attacker than on a per-user credential.
    if not presented or not hmac.compare_digest(presented, expected):
        raise error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "Invalid dispatch token.")


def require_store(store: Any) -> Any:
    """Return the operational store, or refuse the request.

    Raises:
        HTTPException: 404 if this deployment has no user database, in which
            case there are no identities to federate in the first place.
    """
    if store is None:
        raise error(
            status.HTTP_404_NOT_FOUND,
            "internal_api_not_configured",
            "This deployment has no user database, so it cannot serve internal lookups.",
        )
    return store
