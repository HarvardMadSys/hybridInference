"""Internal endpoints called by the console's server-side route handlers.

Written originally for Nginx ``auth_request`` subrequests. Host nginx was
purged from both deployments on 2026-09-13 — the Cloudflare tunnel now routes
straight to the frontend container, and ``apps/frontend/next.config.js`` does
the path routing — so no subrequest reaches anything here any more. The one
route below outlived that because its caller did: the gate moved into a Next.js
route handler rather than disappearing. See ``verify_admin``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from serving.auth.browser_sessions import (
    get_refresh_token_cookie,
    hash_refresh_token,
    resolve_browser_session,
    session_live,
)
from serving.servers.deps import get_operational_store
from serving.utils.logging import get_logger

router = APIRouter(prefix="/internal", tags=["Internal"])
logger = get_logger(__name__)


async def _validate_session(refresh_token: str | None, op_store: Any) -> dict[str, Any]:
    """Validate a refresh_token cookie and return the associated user row.

    Raises HTTPException on any authentication failure.
    """
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available.")

    token_hash = hash_refresh_token(refresh_token)
    session_row = await op_store.get_session_by_token_hash(token_hash)

    if not session_row:
        raise HTTPException(status_code=401, detail="Invalid session.")

    if session_row["revoked"]:
        raise HTTPException(status_code=401, detail="Session revoked.")

    if session_row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired.")

    user_row = await op_store.get_user_by_id(session_row["user_id"])

    if not user_row:
        raise HTTPException(status_code=401, detail="User not found.")

    # Suspending an account revokes its API key, not its session rows, so a
    # suspended (or never approved) admin can still hold a live session here.
    # The browser-session path refuses them through ``session_live``; this
    # path must too, or such an admin keeps opening pgAdmin.
    if user_row.get("status") != "active":
        raise HTTPException(status_code=401, detail="Account is not active.")

    return user_row


@router.get("/verify-admin")
async def verify_admin(
    request: Request,
    op_store=Depends(get_operational_store),
) -> Response:
    """Verify that the caller has admin role via their session cookie.

    Gates access to pgAdmin. The caller is the console's pgAdmin proxy,
    ``apps/frontend/src/app/pgadmin/[[...path]]/route.ts``, which fetches this
    over the internal network with the browser's cookie and admits only on an
    explicit 200. It is a route handler and not a rewrite because a rewrite
    cannot authenticate, which is also why this endpoint survived the removal of
    the Nginx ``auth_request`` block that used to call it.

    With several accounts signed in on one browser, it is enough that one of
    them is an administrator whose session is live: pgAdmin opens in a tab of
    its own, which has no notion of the account the console is showing. The
    account that admitted the browser is logged.

    Returns 200 for admin only, 401/403 otherwise. Read-only: this answer is
    not relayed to the browser, so it can neither rotate nor upgrade a cookie.
    """
    refresh_token = get_refresh_token_cookie(request)
    if refresh_token and op_store:
        browser = await resolve_browser_session(op_store, request, upgrade=False)
        if browser is not None:
            live = [account for account in browser.accounts if session_live(account)]
            admin = next((a for a in live if (a["role"] or "free") == "admin"), None)
            if admin is not None:
                logger.info("verify_admin admitted browser %s as %s", browser.id, admin["user_id"])
                return Response(status_code=200)
            if live:
                raise HTTPException(status_code=403, detail="Admin access required.")
            raise HTTPException(status_code=401, detail="Invalid session.")

    # A cookie from before browser sessions names one account's session.
    user = await _validate_session(refresh_token, op_store)

    if (user["role"] or "free") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")

    return Response(status_code=200)
