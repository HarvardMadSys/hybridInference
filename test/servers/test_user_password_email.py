"""Tests for user password and email change functionality."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Real argon2 hash for "OldPassword123" - used in mocks
MOCK_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$somebase64salt$somebase64hash"


@pytest.mark.asyncio
async def test_change_password_success(test_app, test_client):
    """Test successful password change."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", return_value=True),
    ):
        # Mock JWT verification to return user info
        mock_verify.return_value = {"sub": "user123", "email": "test@example.com"}

        # Setup mock database
        mock_conn = MagicMock()
        # First call: get user from JWT, second call: get password hash
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "user_id": "user123",
                    "email": "test@example.com",
                    "status": "active",
                    "tier": "free",
                    "email_verified": True,
                },  # User lookup
                {"password_hash": MOCK_PASSWORD_HASH},  # Password hash lookup
            ]
        )
        mock_conn.execute = AsyncMock()
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/user/change-password",
            json={"old_password": "OldPassword123", "new_password": "NewPassword456"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 200
    data = response.json()
    assert "password changed successfully" in data["message"].lower()


@pytest.mark.asyncio
async def test_change_password_wrong_old_password(test_app, test_client):
    """Test password change with incorrect old password."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", return_value=False),
    ):
        # Mock JWT verification
        mock_verify.return_value = {"sub": "user123", "email": "test@example.com"}

        # Setup mock database
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "user_id": "user123",
                    "email": "test@example.com",
                    "status": "active",
                    "tier": "free",
                    "email_verified": True,
                },
                {"password_hash": MOCK_PASSWORD_HASH},
            ]
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/user/change-password",
            json={"old_password": "WrongPassword123", "new_password": "NewPassword456"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 400
    assert "incorrect" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_password_same_as_old(test_app, test_client):
    """Test password change with new password same as old."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", side_effect=[True, True]),
    ):
        # Mock JWT verification
        mock_verify.return_value = {"sub": "user123", "email": "test@example.com"}

        # Setup mock database
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "user_id": "user123",
                    "email": "test@example.com",
                    "status": "active",
                    "tier": "free",
                    "email_verified": True,
                },
                {"password_hash": MOCK_PASSWORD_HASH},
            ]
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/user/change-password",
            json={"old_password": "SamePassword123", "new_password": "SamePassword123"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 400
    assert "different" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_password_weak_new_password(test_app, test_client):
    """Test password change with weak new password."""
    with patch("serving.utils.jwt.verify_access_token") as mock_verify:
        # Mock JWT verification
        mock_verify.return_value = {"sub": "user123", "email": "test@example.com"}

        # Setup mock database for user lookup
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "user_id": "user123",
                "email": "test@example.com",
                "status": "active",
                "tier": "free",
                "email_verified": True,
            }
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/user/change-password",
            json={
                "old_password": "OldPassword123",
                "new_password": "weak",  # Too short
            },
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_change_email_success(test_app, test_client):
    """Test successful email change."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", return_value=True),
        patch("serving.utils.email.is_email_enabled", return_value=True),
        patch("serving.utils.email.send_verification_email", return_value=True),
    ):
        # Mock JWT verification
        mock_verify.return_value = {"sub": "user123", "email": "old@example.com"}

        # Setup mock database
        mock_conn = MagicMock()
        # Calls: user lookup, get current email/password, check if new email exists
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "user_id": "user123",
                    "email": "old@example.com",
                    "status": "active",
                    "tier": "free",
                    "email_verified": True,
                },  # User lookup
                {"email": "old@example.com", "password_hash": MOCK_PASSWORD_HASH},  # Current user
                None,  # New email not in use
            ]
        )
        mock_conn.execute = AsyncMock()
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/user/change-email",
            json={"new_email": "new@example.com", "password": "MyPassword123"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 200
    data = response.json()
    assert "email changed successfully" in data["message"].lower()
    assert data["new_email"] == "new@example.com"


@pytest.mark.asyncio
async def test_change_email_wrong_password(test_app, test_client):
    """Test email change with incorrect password."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", return_value=False),
    ):
        # Mock JWT verification
        mock_verify.return_value = {"sub": "user123", "email": "old@example.com"}

        # Setup mock database
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "user_id": "user123",
                    "email": "old@example.com",
                    "status": "active",
                    "tier": "free",
                    "email_verified": True,
                },
                {"email": "old@example.com", "password_hash": MOCK_PASSWORD_HASH},
            ]
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/user/change-email",
            json={"new_email": "new@example.com", "password": "WrongPassword123"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 400
    assert "incorrect" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_email_same_as_current(test_app, test_client):
    """Test email change with same email as current."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", return_value=True),
    ):
        # Mock JWT verification
        mock_verify.return_value = {"sub": "user123", "email": "same@example.com"}

        # Setup mock database
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "user_id": "user123",
                    "email": "same@example.com",
                    "status": "active",
                    "tier": "free",
                    "email_verified": True,
                },
                {"email": "same@example.com", "password_hash": MOCK_PASSWORD_HASH},
            ]
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/user/change-email",
            json={"new_email": "same@example.com", "password": "MyPassword123"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 400
    assert "different" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_email_already_in_use(test_app, test_client):
    """Test email change with email already registered."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", return_value=True),
    ):
        # Mock JWT verification
        mock_verify.return_value = {"sub": "user123", "email": "old@example.com"}

        # Setup mock database
        mock_conn = MagicMock()
        # Calls: user lookup, get current email/password, check if new email exists
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "user_id": "user123",
                    "email": "old@example.com",
                    "status": "active",
                    "tier": "free",
                    "email_verified": True,
                },  # User lookup
                {"email": "old@example.com", "password_hash": MOCK_PASSWORD_HASH},  # Current user
                {"id": "other_user"},  # New email already in use
            ]
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/user/change-email",
            json={"new_email": "taken@example.com", "password": "MyPassword123"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 409
    assert "already registered" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_password_requires_auth(test_client):
    """Test that change password requires authentication."""
    # Don't provide Authorization header
    response = await test_client.post(
        "/user/change-password",
        json={"old_password": "OldPassword123", "new_password": "NewPassword456"},
    )

    # Without authentication, should get 401 Unauthorized
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_change_email_requires_auth(test_client):
    """Test that change email requires authentication."""
    # Don't provide Authorization header
    response = await test_client.post(
        "/user/change-email", json={"new_email": "new@example.com", "password": "MyPassword123"}
    )

    # Without authentication, should get 401 Unauthorized
    assert response.status_code == 401
