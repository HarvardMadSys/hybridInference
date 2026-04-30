"""Test that email verification is required for login.

This test ensures that users with unverified emails cannot login,
fixing the security vulnerability where unverified users could access the system.
"""

import os

import pytest

# Mark all tests in this file as requiring database
pytestmark = pytest.mark.asyncio


# New helper and fixture
@pytest.fixture
def email_verification_flag(monkeypatch):
    """Temporarily set SIGNUP_REQUIRE_EMAIL_VERIFICATION for a test."""

    def _setter(enabled: bool) -> None:
        monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1" if enabled else "0")

    return _setter


async def set_email_verified(require_db, user_id: str, verified: bool) -> None:
    """Set user's email_verified flag.

    Args:
      require_db: DB fixture ensuring pool availability.
      user_id: Target user ID.
      verified: Desired verification state.
    """
    async with require_db.pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            "UPDATE users SET email_verified = $1 WHERE id = $2",
            verified,
            user_id,
        )


async def test_unverified_user_cannot_login(auth_client, require_db, email_verification_flag):
    """Test that users with unverified emails cannot login.

    This test verifies the security fix that prevents unverified users
    from logging in when SIGNUP_REQUIRE_EMAIL_VERIFICATION is enabled.
    """
    # Enable email verification for this test
    email_verification_flag(True)

    # Sign up a new user
    signup_data = {
        "email": f"unverified_{os.urandom(4).hex()}@signuptest.dev",
        "password": "SecurePass123!",
        "user_name": "Unverified User",
    }

    response = await auth_client.post("/auth/signup", json=signup_data)
    assert response.status_code == 201

    # Try to login without verifying email
    login_data = {
        "email": signup_data["email"],
        "password": signup_data["password"],
    }

    response = await auth_client.post("/auth/login", json=login_data)
    assert response.status_code == 403
    assert "Email not verified" in response.text


async def test_verified_user_can_login(auth_client, require_db, email_verification_flag):
    """Test that users with verified emails can login.

    This test ensures that the email verification check doesn't break
    normal login flow for verified users.
    """
    # Enable email verification for this test
    email_verification_flag(True)

    # Sign up a new user
    signup_data = {
        "email": f"verified_{os.urandom(4).hex()}@signuptest.dev",
        "password": "SecurePass123!",
        "user_name": "Verified User",
    }

    response = await auth_client.post("/auth/signup", json=signup_data)
    assert response.status_code == 201
    user_id = response.json()["user_id"]

    # Manually verify the user's email (simulating email verification)
    await set_email_verified(require_db, user_id, True)

    # Try to login after verifying email
    login_data = {
        "email": signup_data["email"],
        "password": signup_data["password"],
    }

    response = await auth_client.post("/auth/login", json=login_data)
    assert response.status_code == 200
    assert "access_token" in response.json()


async def test_unverified_user_cannot_refresh_token(
    auth_client, require_db, email_verification_flag
):
    """Test that unverified users cannot refresh their tokens.

    This test ensures that if a user's email becomes unverified
    (e.g., after changing email), they cannot refresh their tokens.
    """
    # Enable email verification for this test
    email_verification_flag(True)

    # Sign up a new user
    signup_data = {
        "email": f"unverified2_{os.urandom(4).hex()}@signuptest.dev",
        "password": "SecurePass123!",
        "user_name": "Unverified User 2",
    }

    response = await auth_client.post("/auth/signup", json=signup_data)
    assert response.status_code == 201
    user_id = response.json()["user_id"]

    # Manually verify email to allow initial login
    await set_email_verified(require_db, user_id, True)

    # Login to get tokens
    login_data = {
        "email": signup_data["email"],
        "password": signup_data["password"],
    }

    response = await auth_client.post("/auth/login", json=login_data)
    assert response.status_code == 200

    # Now unverify the email (simulating email change without verification)
    await set_email_verified(require_db, user_id, False)

    # Try to refresh token with unverified email
    response = await auth_client.post("/auth/refresh")
    assert response.status_code == 403
    assert "Email not verified" in response.text


async def test_email_verification_can_be_disabled(auth_client, require_db, email_verification_flag):
    """Test that email verification requirement can be disabled via env var.

    This test ensures backward compatibility - when email verification
    is disabled, users can login without verifying their email.
    """
    # Disable email verification for this test
    email_verification_flag(False)

    # Sign up a new user
    signup_data = {
        "email": f"noverify_{os.urandom(4).hex()}@signuptest.dev",
        "password": "SecurePass123!",
        "user_name": "No Verify User",
    }

    response = await auth_client.post("/auth/signup", json=signup_data)
    assert response.status_code == 201

    # Try to login without verifying email (should work when verification is disabled)
    login_data = {
        "email": signup_data["email"],
        "password": signup_data["password"],
    }

    response = await auth_client.post("/auth/login", json=login_data)
    assert response.status_code == 200
    assert "access_token" in response.json()


async def test_unverified_user_cannot_access_protected_endpoint(
    auth_client, require_db, email_verification_flag
):
    """Test that unverified users cannot call protected user endpoints."""
    email_verification_flag(True)

    signup_data = {
        "email": f"protected_{os.urandom(4).hex()}@signuptest.dev",
        "password": "SecurePass123!",
        "user_name": "Protected User",
    }
    response = await auth_client.post("/auth/signup", json=signup_data)
    assert response.status_code == 201
    user_id = response.json()["user_id"]

    await set_email_verified(require_db, user_id, True)

    login_data = {
        "email": signup_data["email"],
        "password": signup_data["password"],
    }
    response = await auth_client.post("/auth/login", json=login_data)
    assert response.status_code == 200
    access_token = response.json()["access_token"]

    await set_email_verified(require_db, user_id, False)

    response = await auth_client.get(
        "/user/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 403
    assert "Email not verified" in response.text
