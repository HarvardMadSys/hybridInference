"""Internal endpoints for Nginx auth_request subrequests."""

from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response

from serving.config.settings import is_admin_email
from serving.servers.deps import get_db_logger
from serving.servers.routers.auth_routes import hash_refresh_token

router = APIRouter(prefix="/internal", tags=["Internal"])


@router.get("/verify-grafana")
async def verify_grafana(
    refresh_token: str | None = Cookie(None),
    db_logger=Depends(get_db_logger),
) -> Response:
    """Verify that the caller is an admin via their refresh_token cookie.

    Used by Nginx ``auth_request`` to gate access to Grafana.
    Returns 200 for admins, 401/403 otherwise.
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

    # Look up user email
    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT email FROM users WHERE id = $1",
            session_row["user_id"],
        )

    if not user_row:
        raise HTTPException(status_code=401, detail="User not found.")

    if not is_admin_email(user_row["email"]):
        raise HTTPException(status_code=403, detail="Admin access required.")

    return Response(status_code=200)
