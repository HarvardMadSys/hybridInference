"""Internal endpoints for Nginx auth_request subrequests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response

from serving.servers.deps import get_db_logger
from serving.servers.routers.auth_routes import hash_refresh_token

router = APIRouter(prefix="/internal", tags=["Internal"])


async def _validate_session(refresh_token: str | None, db_logger: Any) -> dict[str, Any]:
    """Validate a refresh_token cookie and return the associated user row.

    Raises HTTPException on any authentication failure.
    """
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    if not db_logger or not db_logger.pool:
        raise HTTPException(status_code=500, detail="Database not available.")

    token_hash = hash_refresh_token(refresh_token)

    async with db_logger.pool.acquire() as conn:
        session_row = await conn.fetchrow(
            """
            SELECT user_id, expires_at, revoked
            FROM auth_sessions
            WHERE refresh_token_hash = $1
            """,
            token_hash,
        )

    if not session_row:
        raise HTTPException(status_code=401, detail="Invalid session.")

    if session_row["revoked"]:
        raise HTTPException(status_code=401, detail="Session revoked.")

    if session_row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired.")

    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT email, role FROM users WHERE id = $1",
            session_row["user_id"],
        )

    if not user_row:
        raise HTTPException(status_code=401, detail="User not found.")

    return dict(user_row)


@router.get("/verify-admin")
async def verify_admin(
    refresh_token: str | None = Cookie(None),
    db_logger=Depends(get_db_logger),
) -> Response:
    """Verify that the caller has admin role via their refresh_token cookie.

    Used by Nginx ``auth_request`` to gate access to pgAdmin.
    Returns 200 for admin only, 401/403 otherwise.
    """
    user = await _validate_session(refresh_token, db_logger)

    if (user["role"] or "free") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")

    return Response(status_code=200)
