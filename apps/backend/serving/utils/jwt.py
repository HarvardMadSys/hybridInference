"""JWT token generation and validation utilities."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from ulid import ULID


def get_jwt_secret() -> str:
    """Get the JWT secret from the same settings validated at startup.

    Raises:
        ValueError: If JWT_SECRET_KEY is not set.
    """
    from serving.config.settings import get_settings

    secret = get_settings().jwt_secret_key
    if not secret.strip():
        raise ValueError("JWT_SECRET_KEY must be set in environment")
    return secret


def get_jwt_algorithm() -> str:
    """Get the JWT signing algorithm (``JWT_ALGORITHM``, default HS256)."""
    from serving.config.settings import get_settings

    return get_settings().jwt_algorithm


def get_access_token_expire_minutes() -> int:
    """Get the access token lifetime in minutes (default 15)."""
    from serving.config.settings import get_settings

    return get_settings().jwt_access_token_expire_minutes


def get_refresh_token_expire_days() -> int:
    """Get refresh token expiration time in days (default: 30).

    Reduced from 365 to 30 to limit the blast radius of a stolen refresh
    token. /auth/refresh rotates the token JTI on every call, but the
    underlying session (``OperationalStore.rotate_session``) keeps its
    original ``expires_at``. A session created today therefore hard-expires
    in ~30 days regardless of activity — users will see a forced re-login.
    """
    from serving.config.settings import settings

    return settings.jwt_refresh_token_expire_days


def generate_ulid() -> str:
    """Generate a new ULID string."""
    return str(ULID())


def generate_session_id() -> str:
    """Generate a session ID with sess_ prefix."""
    return f"sess_{generate_ulid()}"


def generate_jti() -> str:
    """Generate a JWT ID (jti) with jwt_ prefix."""
    return f"jwt_{generate_ulid()}"


def create_access_token(
    user_id: str,
    email: str | None,
    session_id: str | None = None,
    expires_delta: timedelta | None = None,
    is_admin: bool = False,
    role: str = "free",
) -> tuple[str, str]:
    """Create a JWT access token.

    Args:
        user_id: User ID to encode in token.
        email: User email to encode in token; an account without one (the
            first-run setup administrator) gets an empty ``email`` claim.
        session_id: Session ID for token rotation (optional).
        expires_delta: Custom expiration time (default: from settings).
        is_admin: Whether user has admin privileges.
        role: User permission role (free/pro/internal/admin).

    Returns:
        Tuple of (token_string, jti).
    """
    if expires_delta is None:
        expires_delta = timedelta(minutes=get_access_token_expire_minutes())

    jti = generate_jti()
    sid = session_id or generate_session_id()

    now = datetime.now(timezone.utc)
    expire = now + expires_delta

    payload = {
        "sub": user_id,
        "email": email or "",
        "role": role,
        "is_admin": is_admin,
        "jti": jti,
        "sid": sid,
        "iat": now,
        "exp": expire,
    }

    token = jwt.encode(payload, get_jwt_secret(), algorithm=get_jwt_algorithm())
    return token, jti


def create_refresh_token() -> str:
    """Create a random refresh token (not JWT, just a random string).

    Returns:
        URL-safe random token string.
    """
    return secrets.token_urlsafe(64)


def verify_access_token(token: str) -> dict[str, Any]:
    """Verify and decode a JWT access token.

    Args:
        token: JWT token string to verify.

    Returns:
        Decoded token payload.

    Raises:
        jwt.ExpiredSignatureError: If token is expired.
        jwt.InvalidTokenError: If token is invalid.
    """
    payload = jwt.decode(token, get_jwt_secret(), algorithms=[get_jwt_algorithm()])
    return payload
