"""FastAPI dependency helpers for application services.

This module exposes small dependency functions that retrieve shared services
from ``app.state``. Keeping these helpers thin makes route handlers easy to
test and avoids hidden global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import jwt
from fastapi import Depends, Header, HTTPException, Request

if TYPE_CHECKING:
    from routing.routers import FixedRouter, NimbusRouter
    from serving.observability.user_stats import UserStatsCollector
    from serving.storage.database import DatabaseLogger

    from .rate_limiter import PersistentRateLimiter


@dataclass
class AppServices:
    """Typed container for application-wide services.

    Using a dataclass improves discoverability and avoids fragile string keys
    when accessing ``app.state``.
    """

    router: FixedRouter
    embedding_adapters: dict[str, Any] | None = None
    rate_limiter: PersistentRateLimiter | None = None
    db_logger: DatabaseLogger | None = None
    nimbus_router: NimbusRouter | None = None
    user_stats_collector: UserStatsCollector | None = None


def get_services(request: Request) -> AppServices:
    """Return the shared services object from the application state."""
    return request.app.state.services  # type: ignore[attr-defined]


def get_router(services: AppServices = Depends(get_services)) -> FixedRouter:
    """Dependency to obtain the FixedRouter."""
    return services.router


def get_nimbus_router(services: AppServices = Depends(get_services)) -> NimbusRouter | None:
    """Dependency to obtain the NimbusRouter (if enabled)."""
    return services.nimbus_router


def get_embedding_adapters(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    """Dependency to obtain the embedding adapters dict."""
    return services.embedding_adapters or {}


def get_rate_limiter(
    services: AppServices = Depends(get_services),
) -> PersistentRateLimiter | None:
    """Dependency to obtain the rate limiter (if configured)."""
    return services.rate_limiter


def get_db_logger(
    services: AppServices = Depends(get_services),
) -> DatabaseLogger | None:
    """Dependency to obtain the database logger (if configured)."""
    return services.db_logger


def is_database_connected(db_logger: DatabaseLogger | None) -> bool:
    """Check if database connection is active.

    Args:
        db_logger: DatabaseLogger instance from get_db_logger dependency

    Returns:
        True if database is connected and pool is available, False otherwise
    """
    return db_logger is not None and db_logger.pool is not None


async def get_current_user(
    authorization: str | None = Header(None),
    db_logger=Depends(get_db_logger),
) -> dict[str, Any]:
    """Verify JWT token and return current user context.

    This dependency is used for user dashboard endpoints that require authentication.

    Args:
        authorization: Authorization header with Bearer token.
        db_logger: Database logger instance.

    Returns:
        User context dictionary with user_id, email, tier, etc.

    Raises:
        HTTPException: 401 if token is missing, invalid, or expired.
    """
    from serving.utils.jwt import verify_access_token

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing authentication token. Use 'Authorization: Bearer {token}' header.",
        )

    token = authorization[7:]  # Strip "Bearer " prefix

    try:
        payload = verify_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Token has expired. Please refresh your token or login again.",
        ) from None
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token.",
        ) from None

    # Extract user info from token
    user_id = payload.get("sub")
    email = payload.get("email")
    tier = payload.get("tier", "free")

    if not user_id or not email:
        raise HTTPException(
            status_code=401,
            detail="Invalid token payload.",
        )

    # Verify user still exists and is active in database
    if not db_logger or not db_logger.pool:
        raise HTTPException(
            status_code=500,
            detail="Database not available for authentication",
        )

    async with db_logger.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            """
            SELECT id, email, status, email_verified
            FROM users
            WHERE id = $1
            """,
            user_id,
        )

    if not user_row:
        raise HTTPException(
            status_code=401,
            detail="User not found.",
        )

    if user_row["status"] != "active":
        raise HTTPException(
            status_code=403,
            detail=f"Account is {user_row['status']}. Please contact support.",
        )

    # Return user context
    return {
        "user_id": user_id,
        "email": email,
        "tier": tier,
        "email_verified": user_row["email_verified"],
        "status": user_row["status"],
    }
