"""Test factories for authentication system."""

import secrets
from datetime import datetime, timezone
from typing import Any

from serving.utils.jwt import generate_ulid
from serving.utils.password import hash_password


def create_test_user(**overrides: Any) -> dict[str, Any]:
    """Create a test user dict with defaults.

    Args:
        **overrides: Override default values

    Returns:
        User data dict suitable for database insertion
    """
    user_id = generate_ulid()
    email = f"test_{user_id[:8]}@signuptest.dev"

    defaults = {
        "id": user_id,
        "email": email,
        "password": "SecurePass123!",  # Plain text for testing
        "password_hash": hash_password("SecurePass123!"),
        "user_name": f"Test User {user_id[:8]}",
        "status": "active",
        "email_verified": True,
        "created_at": datetime.now(timezone.utc),
        "last_login_at": None,
    }

    result = dict(defaults)
    result.update(overrides)

    # If password is overridden, update password_hash
    if "password" in overrides and "password_hash" not in overrides:
        result["password_hash"] = hash_password(overrides["password"])

    return result


def create_signup_request(**overrides: Any) -> dict[str, Any]:
    """Create a signup request dict with defaults.

    Args:
        **overrides: Override default values

    Returns:
        Signup request dict
    """
    unique_id = secrets.token_hex(4)

    defaults = {
        "email": f"newuser_{unique_id}@signuptest.dev",
        "password": "SecurePass123!",
        "user_name": f"New User {unique_id}",
    }

    result = dict(defaults)
    result.update(overrides)
    return result


def create_login_request(**overrides: Any) -> dict[str, Any]:
    """Create a login request dict with defaults.

    Args:
        **overrides: Override default values

    Returns:
        Login request dict
    """
    defaults = {
        "email": "test@example.com",
        "password": "SecurePass123!",
    }

    result = dict(defaults)
    result.update(overrides)
    return result


def create_api_key_data(user_id: str, **overrides: Any) -> dict[str, Any]:
    """Create API key data dict with defaults.

    Args:
        user_id: User ID who owns the key
        **overrides: Override default values

    Returns:
        API key data dict suitable for database insertion
    """
    key_prefix = f"sk-test-{secrets.token_hex(4)}"

    defaults = {
        "key_hash": secrets.token_hex(32),
        "key_prefix": key_prefix,
        "user_id": user_id,
        "account_id": user_id,
        "status": "active",
        "quota_daily_cost_usd": 100.00,
        "tier": "free",
        "created_at": datetime.now(timezone.utc),
        "last_used_at": None,
    }

    result = dict(defaults)
    result.update(overrides)
    return result


def create_verification_token(user_id: str, **overrides: Any) -> dict[str, Any]:
    """Create email verification token dict with defaults.

    Args:
        user_id: User ID
        **overrides: Override default values

    Returns:
        Verification token data dict
    """
    from datetime import timedelta

    defaults = {
        "token": secrets.token_urlsafe(32),
        "user_id": user_id,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=24),
        "used_at": None,
    }

    result = dict(defaults)
    result.update(overrides)
    return result


def create_auth_session(user_id: str, **overrides: Any) -> dict[str, Any]:
    """Create auth session dict with defaults.

    Args:
        user_id: User ID
        **overrides: Override default values

    Returns:
        Auth session data dict
    """
    from datetime import timedelta

    defaults = {
        "session_id": generate_ulid(),
        "user_id": user_id,
        "refresh_token_hash": secrets.token_hex(32),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        "last_used_jti": None,
        "revoked": False,
        "created_at": datetime.now(timezone.utc),
    }

    result = dict(defaults)
    result.update(overrides)
    return result
