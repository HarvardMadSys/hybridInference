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

from serving.config.settings import get_settings
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

logger = get_logger(__name__)

if TYPE_CHECKING:
    from routing.executor import RouteExecutor
    from routing.manager import RoutingManager
    from routing.model_router_registry import ModelRouterRegistry
    from serving.config.model_visibility import ModelVisibilityResolver
    from serving.observability.alert_rules import AlertEngine
    from serving.servers.routers.completions_logging import CompletionsLogger
    from serving.storage.base import LogStore, OperationalStore
    from serving.storage.database import DatabaseLogger

    from .concurrency import UserConcurrencyLimiter


@dataclass
class AppServices:
    """Typed container for application-wide services.

    Using a dataclass improves discoverability and avoids fragile string keys
    when accessing ``app.state``.
    """

    router: RouteExecutor
    embedding_adapters: dict[str, Any] | None = None
    db_logger: DatabaseLogger | None = None
    operational_store: OperationalStore | None = None
    log_store: LogStore | None = None
    routing_manager: RoutingManager | None = None
    model_router_registry: ModelRouterRegistry | None = None
    model_visibility_resolver: ModelVisibilityResolver | None = None
    user_concurrency_limiter: UserConcurrencyLimiter | None = None
    alert_engine: AlertEngine | None = None
    runtime_settings: Any | None = None
    completions_logger: CompletionsLogger | None = None


def get_services(request: Request) -> AppServices:
    """Return the shared services object from the application state."""
    return request.app.state.services  # type: ignore[attr-defined]


def get_router(services: AppServices = Depends(get_services)) -> RouteExecutor:
    """Dependency to obtain the RouteExecutor."""
    return services.router


def get_embedding_adapters(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    """Dependency to obtain the embedding adapters dict."""
    return services.embedding_adapters or {}


def get_db_logger(
    services: AppServices = Depends(get_services),
) -> DatabaseLogger | None:
    """Dependency to obtain the database logger (if configured)."""
    return services.db_logger


def get_operational_store(
    services: AppServices = Depends(get_services),
) -> OperationalStore | None:
    """Dependency to obtain the operational store (if configured)."""
    return services.operational_store


def get_log_store(
    services: AppServices = Depends(get_services),
) -> LogStore | None:
    """Dependency to obtain the log store (if configured)."""
    return services.log_store


def get_user_concurrency_limiter(
    services: AppServices = Depends(get_services),
) -> UserConcurrencyLimiter | None:
    """Dependency to obtain the per-user concurrency limiter."""
    return services.user_concurrency_limiter


def get_model_router_registry(
    services: AppServices = Depends(get_services),
) -> ModelRouterRegistry | None:
    """Dependency to obtain the ModelRouterRegistry (if configured)."""
    return services.model_router_registry


def get_model_visibility_resolver(
    services: AppServices = Depends(get_services),
) -> ModelVisibilityResolver | None:
    """Dependency to obtain the ModelVisibilityResolver (if configured)."""
    return getattr(services, "model_visibility_resolver", None)


def get_completions_logger(
    services: AppServices = Depends(get_services),
) -> CompletionsLogger:
    """Dependency to obtain the ``CompletionsLogger``.

    Lazily constructs a logger if bootstrap didn't initialize one (e.g., in
    tests that build ``AppServices`` directly without going through
    ``bootstrap.initialize``), memoizing the instance on ``services`` so
    subsequent requests reuse it. Returning a ready-to-use instance keeps
    the handler free of None checks.
    """
    if services.completions_logger is None:
        from serving.servers.routers.completions_logging import CompletionsLogger as _CL

        services.completions_logger = _CL(
            log_store=services.log_store,
            model_router_registry=services.model_router_registry,
        )
    return services.completions_logger


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
    op_store=Depends(get_operational_store),
) -> dict[str, Any]:
    """Verify JWT token and return current user context.

    This dependency is used for user dashboard endpoints that require authentication.

    Args:
        authorization: Authorization header with Bearer token.
        op_store: Operational store instance.

    Returns:
        User context dictionary with user_id, email, role, etc.

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

    if not user_id or not email:
        raise HTTPException(
            status_code=401,
            detail="Invalid token payload.",
        )

    # Verify user still exists and is active in database
    if not op_store:
        raise HTTPException(
            status_code=500,
            detail="Database not available for authentication",
        )

    user_row = await op_store.get_user_by_id(user_id)

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

    require_verification = get_settings().signup_require_email_verification
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        rs = get_runtime_settings_instance()
        require_verification = await rs.get_bool("signup_require_email_verification")
    except (RuntimeError, KeyError):
        pass
    if require_verification and not user_row["email_verified"]:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please check your email for the verification link.",
        )

    # Return user context — use DB email and role (authoritative) instead of JWT claims.
    user_role = user_row["role"] or "free"
    return {
        "user_id": user_id,
        "email": user_row["email"],
        "role": user_role,
        "is_admin": user_role == "admin",
        "email_verified": user_row["email_verified"],
        "status": user_row["status"],
    }


async def require_admin(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Require admin privileges. Raises 403 if user is not an admin.

    Uses the authoritative ``users.role`` from DB (set by get_current_user).
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user


def require_role(min_role: str):
    """Dependency factory: require a minimum role level.

    Usage: ``Depends(require_role("internal"))``
    """

    async def _check(
        current_user: dict[str, Any] = Depends(get_current_user),
    ) -> dict[str, Any]:
        from serving.config.settings import has_role

        if not has_role(current_user.get("role", "free"), min_role):
            raise HTTPException(status_code=403, detail=f"Requires role '{min_role}' or higher.")
        return current_user

    return _check


async def verify_admin_access(
    request: Request,
    authorization: str | None = Header(None),
    op_store=Depends(get_operational_store),
) -> str:
    """Unified admin auth: accept either JWT (admin user) or ADMIN_TOKEN.

    This dependency allows admin endpoints to be called from both the
    frontend dashboard (JWT) and scripts/legacy admin UI (ADMIN_TOKEN).

    Returns:
        Admin identifier string (email for JWT auth, IP for token auth).
    """
    from serving.utils.jwt import verify_access_token

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing authentication. Provide a JWT or admin token via Authorization header.",
        )

    token = authorization[7:]

    try:
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        email = payload.get("email", "")

        if op_store and user_id:
            user_row = await op_store.get_user_by_id(user_id)
            if not user_row or user_row["status"] != "active":
                raise HTTPException(status_code=403, detail="Admin account is no longer active.")
            require_verification = get_settings().signup_require_email_verification
            try:
                from serving.config.runtime_settings import get_runtime_settings_instance

                rs = get_runtime_settings_instance()
                require_verification = await rs.get_bool("signup_require_email_verification")
            except (RuntimeError, KeyError):
                pass
            if require_verification and not user_row["email_verified"]:
                raise HTTPException(
                    status_code=403,
                    detail="Email not verified. Please verify your email to continue.",
                )
            email = user_row["email"]
            if (user_row["role"] or "free") != "admin":
                raise HTTPException(status_code=403, detail="Admin access required.")
        else:
            # DB unavailable — fail closed.  Admin endpoints require
            # authoritative role verification from the database.
            logger.warning(
                "verify_admin_access: DB unavailable, refusing admin access (fail-closed)"
            )
            raise HTTPException(
                status_code=503,
                detail="Database unavailable; cannot verify admin role. Try again later.",
            )

        return email
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        pass

    # Fall back to ADMIN_TOKEN
    admin_token = get_settings().admin_token
    if not admin_token:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token.",
        )

    import hmac as _hmac

    if not _hmac.compare_digest(token, admin_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token.",
        )

    client_ip = get_client_ip(request)
    return client_ip if client_ip != "unknown" else "admin-token"
