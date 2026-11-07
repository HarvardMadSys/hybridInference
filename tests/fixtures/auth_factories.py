"""Test factories for authentication system."""

import secrets
from datetime import UTC, datetime
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
        "created_at": datetime.now(UTC),
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
        "accepted_tos": True,
    }

    result = dict(defaults)
    result.update(overrides)
    return result
