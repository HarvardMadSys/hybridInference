"""JWT token generation and validation utilities."""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from ulid import ULID


def get_jwt_secret() -> str:
    """Get JWT secret key from environment.

    Raises:
        ValueError: If JWT_SECRET_KEY is not set.
    """
    secret = os.getenv("JWT_SECRET_KEY", "")
    if not secret:
        raise ValueError("JWT_SECRET_KEY must be set in environment")
    return secret


def get_jwt_algorithm() -> str:
    """Get JWT algorithm from environment (default: HS256)."""
    return os.getenv("JWT_ALGORITHM", "HS256")


def get_access_token_expire_minutes() -> int:
    """Get access token expiration time in minutes (default: 15)."""
    return int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "15"))


def get_refresh_token_expire_days() -> int:
    """Get refresh token expiration time in days (default: 365)."""
    return int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "365"))


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
    email: str,
    session_id: str | None = None,
    expires_delta: timedelta | None = None,
    is_admin: bool = False,
    role: str = "free",
) -> tuple[str, str]:
    """Create a JWT access token.

    Args:
        user_id: User ID to encode in token.
        email: User email to encode in token.
        session_id: Session ID for token rotation (optional).
        expires_delta: Custom expiration time (default: from env).
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
        "email": email,
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


def decode_token_without_verification(token: str) -> dict[str, Any] | None:
    """Decode JWT token without verification (for debugging/logging only).

    Args:
        token: JWT token string to decode.

    Returns:
        Decoded payload or None if invalid.
    """
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return None
