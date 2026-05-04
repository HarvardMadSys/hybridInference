"""API key authentication and quota enforcement."""

import hashlib
import hmac
import secrets
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet
from fastapi import Depends, Header, HTTPException, Request

from serving.config.settings import get_settings
from serving.observability.metrics import (
    API_MODEL_REQUESTS,
    DATABASE_CONNECTED,
    normalize_model_label,
    normalize_provider_label,
)
from serving.servers.deps import get_db_logger, get_log_store, get_operational_store
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

logger = get_logger(__name__)
QUOTA_CONTACT_EMAIL = "admin@freeinference.org"


def is_user_auth_enabled() -> bool:
    """Return whether API-key user auth is enabled.

    Fail-closed by default: auth is enabled unless ``USER_AUTH_ENABLED``
    is explicitly set to a falsy value (parsed by Pydantic).
    """
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        rs = get_runtime_settings_instance()
        found, value = rs.get_cached("user_auth_enabled")
        if found:
            return bool(value)
    except (RuntimeError, KeyError):
        pass
    return get_settings().user_auth_enabled


def generate_api_key() -> str:
    """Generate a new API key with format: hyi-{32 random bytes}."""
    random_part = secrets.token_urlsafe(32)
    return f"hyi-{random_part}"


def hash_api_key(plaintext_key: str) -> str:
    """Hash an API key using HMAC-SHA256 keyed by ``API_KEY_SECRET``."""
    secret = get_settings().api_key_secret.encode()
    if not secret:
        raise ValueError("API_KEY_SECRET must be set in environment")
    return hmac.new(secret, plaintext_key.encode(), hashlib.sha256).hexdigest()


def _api_key_cipher() -> Fernet:
    secret = get_settings().api_key_secret.encode()
    if not secret:
        raise ValueError("API_KEY_SECRET must be set in environment")
    key = urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_api_key(plaintext_key: str) -> str:
    """Encrypt an API key for user-facing display later."""
    return _api_key_cipher().encrypt(plaintext_key.encode()).decode()


def decrypt_api_key(encrypted_key: str | None) -> str | None:
    """Decrypt a stored API key, returning None for legacy rows."""
    if not encrypted_key:
        return None
    return _api_key_cipher().decrypt(encrypted_key.encode()).decode()


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a, b)


async def verify_api_key(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    op_store=Depends(get_operational_store),
    log_store=Depends(get_log_store),
) -> dict[str, Any]:
    """Verify API key and enforce quotas.

    Returns user context dict with user_id, role, etc.
    Raises HTTPException(401/429) on auth/quota failures.
    """
    # Check if auth is enabled
    if not is_user_auth_enabled():
        # Auth disabled - allow all, mark as anonymous
        return {
            "user_id": "anonymous",
            "role": "admin",
            "authenticated": False,
            "is_admin": True,
        }

    # Extract API key from headers
    api_key = None
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    elif x_api_key:
        api_key = x_api_key

    if not api_key:
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label("unknown"),
            provider=normalize_provider_label("system"),
            status_code="401",
        ).inc()
        logger.warning(
            "auth_failure",
            extra={
                "event": "auth_failure",
                "remote_ip": get_client_ip(request),
                "key_prefix": None,
                "reason": "missing_api_key",
            },
        )
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Use 'Authorization: Bearer hyi-xxx' or 'X-API-Key: hyi-xxx'",
        )

    # Validate key against database
    if not op_store:
        DATABASE_CONNECTED.set(0)
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label("unknown"),
            provider=normalize_provider_label("system"),
            status_code="500",
        ).inc()
        raise HTTPException(status_code=500, detail="Database not available for authentication")

    key_hash = hash_api_key(api_key)

    try:
        user = await op_store.get_auth_context_by_key_hash(key_hash)
        DATABASE_CONNECTED.set(1)
    except Exception:
        DATABASE_CONNECTED.set(0)
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label("unknown"),
            provider=normalize_provider_label("system"),
            status_code="500",
        ).inc()
        raise

    if not user:
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label("unknown"),
            provider=normalize_provider_label("system"),
            status_code="401",
        ).inc()
        logger.warning(
            "auth_failure",
            extra={
                "event": "auth_failure",
                "remote_ip": get_client_ip(request),
                "key_prefix": api_key[:6] if api_key else None,
                "reason": "invalid_api_key",
            },
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key",
        )

    require_verification = get_settings().signup_require_email_verification
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        rs = get_runtime_settings_instance()
        require_verification = await rs.get_bool("signup_require_email_verification")
    except Exception as exc:
        logger.warning(
            "RuntimeSettings lookup for signup_require_email_verification failed; "
            f"falling back to env: {exc}"
        )
    if require_verification and user.get("email") and not user.get("email_verified"):
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label("unknown"),
            provider=normalize_provider_label("system"),
            status_code="403",
        ).inc()
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please verify your email to continue.",
        )

    # Pre-check daily cost quota via operational store counter table
    cost_spent = 0.0
    if op_store:
        cost_spent = await op_store.get_user_cost_today(user["user_id"])

    # Estimate cost for this request
    estimated_cost = 0.01

    # Get quota with fallback for NULL (old rows from migration)
    quota_daily_cost_usd = user.get("quota_daily_cost_usd")
    quota_daily_cost_usd = 1000.0 if quota_daily_cost_usd is None else float(quota_daily_cost_usd)

    # Check cost quota
    if cost_spent + estimated_cost > quota_daily_cost_usd:
        seconds_until_midnight_utc = _seconds_until_utc_midnight()
        quota_reset_at = _next_utc_midnight()
        API_MODEL_REQUESTS.labels(
            model=normalize_model_label("unknown"),
            provider=normalize_provider_label("system"),
            status_code="429",
        ).inc()
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Daily cost quota exceeded",
                "quota_usd": quota_daily_cost_usd,
                "spent_usd": cost_spent,
                "remaining_usd": max(0, quota_daily_cost_usd - cost_spent),
                "reset_at": quota_reset_at.isoformat(),
                "contact_email": QUOTA_CONTACT_EMAIL,
                "message": (
                    f"Need more quota? Email {QUOTA_CONTACT_EMAIL} and explain your use case."
                ),
                "retry_after": seconds_until_midnight_utc,
            },
            headers={
                "Retry-After": str(seconds_until_midnight_utc),
                "X-RateLimit-Limit-Cost": str(quota_daily_cost_usd),
                "X-RateLimit-Remaining-Cost": str(max(0, quota_daily_cost_usd - cost_spent)),
                "X-RateLimit-Reset": str(int(quota_reset_at.timestamp())),
            },
        )

    # Update last_used timestamp (fire and forget)
    await op_store.update_key_last_used(user["id"])

    # Return user context
    user_role = user.get("role") or "free"
    return {
        "user_id": user["user_id"],
        "user_name": user["user_name"],
        "role": user_role,
        "authenticated": True,
        "quota_remaining_cost_usd": quota_daily_cost_usd - cost_spent,
        "is_admin": user_role == "admin",
        # key_hash identifies the specific hyi-xxx key in use (a user may
        # have multiple). Used as the affinity key for multi-key API rotation.
        "auth_key_hash": key_hash,
    }


async def optional_verify_api_key(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    op_store=Depends(get_operational_store),
) -> dict[str, Any] | None:
    """Lightweight identity lookup — no quota check, no last_used_at write.

    Returns a minimal user context (with ``is_admin``) when a valid API key is
    present, or ``None`` when the key is missing/invalid.  Designed for
    read-only endpoints like ``/v1/models`` that need admin visibility without
    side-effects.
    """
    # Auth disabled — treat caller as anonymous admin
    if not is_user_auth_enabled():
        return {"user_id": "anonymous", "role": "admin", "authenticated": False, "is_admin": True}

    # Extract API key from headers
    api_key = None
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    elif x_api_key:
        api_key = x_api_key

    if not api_key:
        return None  # No key supplied — anonymous

    if not op_store:
        logger.warning("optional_verify_api_key: DB unavailable, cannot resolve identity")
        raise HTTPException(status_code=500, detail="Database not available for authentication")

    try:
        key_hash = hash_api_key(api_key)
    except ValueError as exc:
        logger.error("optional_verify_api_key: API_KEY_SECRET not set")
        raise HTTPException(
            status_code=500, detail="Server authentication misconfiguration"
        ) from exc

    try:
        row = await op_store.get_auth_context_lightweight(key_hash)
    except Exception as exc:
        logger.exception("optional_verify_api_key: DB query failed")
        raise HTTPException(status_code=500, detail="Database error during authentication") from exc

    if not row:
        return None  # Key invalid or expired — treat as anonymous

    require_verification = get_settings().signup_require_email_verification
    try:
        from serving.config.runtime_settings import get_runtime_settings_instance

        rs = get_runtime_settings_instance()
        require_verification = await rs.get_bool("signup_require_email_verification")
    except Exception as exc:
        logger.warning(
            "RuntimeSettings lookup for signup_require_email_verification failed; "
            f"falling back to env: {exc}"
        )
    if require_verification and row["email"] and not row["email_verified"]:
        return None

    user_role = row["role"] or "free"
    return {
        "user_id": row["user_id"],
        "role": user_role,
        "authenticated": True,
        "is_admin": user_role == "admin",
        # key_hash identifies the specific hyi-xxx key in use (a user may
        # have multiple). Used as the affinity key for multi-key API rotation.
        "auth_key_hash": key_hash,
    }


def _seconds_until_utc_midnight() -> int:
    """Calculate seconds until next UTC midnight."""
    return int((_next_utc_midnight() - datetime.now(timezone.utc)).total_seconds())


def _next_utc_midnight() -> datetime:
    """Return the next UTC midnight timestamp."""
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


async def verify_admin_token(
    request: Request,
    authorization: str | None = Header(None),
    db_logger=Depends(get_db_logger),
) -> str:
    """Verify admin token from Authorization header.

    Returns admin IP address for audit logging.
    Raises HTTPException(401) if invalid or missing token.

    All /admin/* routes should use this dependency to protect access.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing admin token. Use 'Authorization: Bearer {ADMIN_TOKEN}' header.",
        )

    token = authorization[7:]
    admin_token = get_settings().admin_token

    if not admin_token:
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration: ADMIN_TOKEN environment variable not set",
        )

    # Constant-time comparison to prevent timing attacks
    if not constant_time_compare(token, admin_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid admin token",
        )

    # Extract admin IP for audit logging
    admin_ip = get_client_ip(request)
    return admin_ip


async def log_admin_action(
    db_logger,
    admin_ip: str,
    action: str,
    target_user_id: str | None = None,
    details: dict[str, Any] | None = None,
    success: bool = True,
) -> None:
    """Log admin action to audit trail.

    Accepts either an OperationalStore or a legacy DatabaseLogger. Callers
    are migrating to pass the store directly; during transition both are
    supported.

    Args:
        db_logger: OperationalStore or DatabaseLogger instance
        admin_ip: IP address of admin performing the action
        action: Action type (e.g., 'create_key', 'revoke_key')
        target_user_id: User ID affected by the action (if applicable)
        details: Additional context (will be stored as JSONB)
        success: Whether the action succeeded
    """
    if not db_logger:
        return  # Silently skip if logging not configured

    # Use store method if available (new path)
    if hasattr(db_logger, "log_admin_action"):
        await db_logger.log_admin_action(
            admin_ip=admin_ip,
            action=action,
            target_user_id=target_user_id,
            details=details,
            success=success,
        )
        return

    # Legacy path: raw pool access (will be removed after full migration)
    if not getattr(db_logger, "pool", None):
        return

    import json

    async with db_logger.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_audit_log (admin_ip, action, target_user_id, details, success)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            """,
            admin_ip,
            action,
            target_user_id,
            json.dumps(details) if details else None,
            success,
        )
