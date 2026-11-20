"""Internal endpoints called by the console's server-side route handlers.

Written originally for Nginx ``auth_request`` subrequests. Host nginx was
purged from both deployments on 2026-09-13 — the Cloudflare tunnel now routes
straight to the frontend container, and ``apps/frontend/next.config.js`` does
the path routing — so no subrequest reaches anything here any more. The one
route below outlived that because its caller did: the gate moved into a Next.js
route handler rather than disappearing. See ``verify_admin``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from serving.auth.browser_sessions import (
    get_refresh_token_cookie,
    hash_refresh_token,
    resolve_browser_session,
    session_live,
)
from serving.exceptions import TwoFactorRequiredError
from serving.servers.deps import admit_admin_identity, get_operational_store
from serving.utils.logging import get_logger

router = APIRouter(prefix="/internal", tags=["Internal"])
logger = get_logger(__name__)


async def _resolve_legacy_session(refresh_token: str | None, op_store: Any) -> tuple[str, str]:
    """Resolve a pre-browser refresh cookie to its user and auth-session ID.

    The refresh-token hash lookup authenticates the credential. Session
    liveness and current account authorization are decided by the shared
    administrator-session policy after this returns.
    """
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    if not op_store:
        raise HTTPException(status_code=500, detail="Database not available.")

    token_hash = hash_refresh_token(refresh_token)
    session_row = await op_store.get_session_by_token_hash(token_hash)

    if not session_row:
        raise HTTPException(status_code=401, detail="Invalid session.")

    user_id = session_row["user_id"]
    sid = session_row["sid"]
    if not isinstance(user_id, str) or not user_id or not isinstance(sid, str) or not sid:
        raise HTTPException(status_code=401, detail="Invalid session.")
    return user_id, sid


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
            if not live:
                raise HTTPException(status_code=401, detail="Invalid session.")
            admins = [a for a in live if (a["role"] or "free") == "admin"]
            if not admins:
                raise HTTPException(status_code=403, detail="Admin access required.")
            for admin in admins:
                try:
                    await admit_admin_identity(request, op_store, admin["user_id"], admin["sid"])
                except TwoFactorRequiredError:
                    continue
                except HTTPException as exc:
                    if exc.status_code in {401, 403}:
                        continue
                    raise
                logger.info("verify_admin admitted browser %s as %s", browser.id, admin["user_id"])
                return Response(status_code=200)
            raise HTTPException(status_code=403, detail="Admin access required.")

    # A cookie from before browser sessions names one account's session.
    user_id, sid = await _resolve_legacy_session(refresh_token, op_store)
    await admit_admin_identity(request, op_store, user_id, sid)
    return Response(status_code=200)
