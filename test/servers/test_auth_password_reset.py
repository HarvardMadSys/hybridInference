"""Tests for password reset functionality."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_forgot_password_success(test_app, test_client):
    """Test successful password reset request."""
    # Mock the database operations and email sending
    with (
        patch("serving.utils.email.is_email_enabled", return_value=True),
        patch("serving.utils.email.send_password_reset_email", return_value=True),
    ):
        # Setup mock database on the app's services
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": "user123", "email": "test@example.com"})
        mock_conn.execute = AsyncMock()
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/auth/forgot-password", json={"email": "test@example.com"}
        )

    assert response.status_code == 200
    data = response.json()
    assert "password reset link has been sent" in data["message"].lower()


@pytest.mark.asyncio
async def test_forgot_password_nonexistent_email(test_app, test_client):
    """Test password reset for non-existent email (should still return success)."""
    # Setup mock database - user doesn't exist
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = mock_conn
    test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = AsyncMock()

    response = await test_client.post(
        "/auth/forgot-password", json={"email": "nonexistent@example.com"}
    )

    # Should still return success to prevent email enumeration
    assert response.status_code == 200
    data = response.json()
    assert "password reset link has been sent" in data["message"].lower()


@pytest.mark.asyncio
async def test_reset_password_success(test_app, test_client):
    """Test successful password reset with valid token."""
    # Setup mock database
    mock_conn = MagicMock()

    # Mock token lookup - valid token
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "user_id": "user123",
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            "used_at": None,
        }
    )
    mock_conn.execute = AsyncMock()
    test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = mock_conn
    test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = AsyncMock()

    response = await test_client.post(
        "/auth/reset-password", json={"token": "valid_token_123", "new_password": "NewPassword123"}
    )

    assert response.status_code == 200
    data = response.json()
    assert "password has been reset" in data["message"].lower()


@pytest.mark.asyncio
async def test_reset_password_invalid_token(test_app, test_client):
    """Test password reset with invalid token."""
    # Setup mock database - token not found
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = mock_conn
    test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = AsyncMock()

    response = await test_client.post(
        "/auth/reset-password", json={"token": "invalid_token", "new_password": "NewPassword123"}
    )

    assert response.status_code == 400
    assert "invalid" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reset_password_expired_token(test_app, test_client):
    """Test password reset with expired token."""
    # Setup mock database - expired token
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "user_id": "user123",
            "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),  # Expired
            "used_at": None,
        }
    )
    test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = mock_conn
    test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = AsyncMock()

    response = await test_client.post(
        "/auth/reset-password", json={"token": "expired_token", "new_password": "NewPassword123"}
    )

    assert response.status_code == 400
    assert "expired" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reset_password_already_used_token(test_app, test_client):
    """Test password reset with already used token."""
    # Setup mock database - used token
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "user_id": "user123",
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            "used_at": datetime.now(timezone.utc) - timedelta(minutes=10),  # Already used
        }
    )
    test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = mock_conn
    test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = AsyncMock()

    response = await test_client.post(
        "/auth/reset-password", json={"token": "used_token", "new_password": "NewPassword123"}
    )

    assert response.status_code == 400
    assert "already been used" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reset_password_weak_password(test_app, test_client):
    """Test password reset with weak password."""
    # Setup mock database - valid token
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "user_id": "user123",
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            "used_at": None,
        }
    )
    test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = mock_conn
    test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = AsyncMock()

    response = await test_client.post(
        "/auth/reset-password",
        json={
            "token": "valid_token",
            "new_password": "weak",  # Too short, no uppercase, no numbers
        },
    )

    assert response.status_code == 400
    assert "password" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_resend_verification_success(test_app, test_client):
    """Test successful resend of verification email."""
    with (
        patch("serving.utils.email.is_email_enabled", return_value=True),
        patch("serving.utils.email.send_verification_email", return_value=True),
    ):
        # Setup mock database - unverified user
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={"id": "user123", "email": "test@example.com", "email_verified": False}
        )
        mock_conn.execute = AsyncMock()
        test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = (
            mock_conn
        )
        test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = (
            AsyncMock()
        )

        response = await test_client.post(
            "/auth/resend-verification", json={"email": "test@example.com"}
        )

    assert response.status_code == 200
    data = response.json()
    assert "verification email has been sent" in data["message"].lower()


@pytest.mark.asyncio
async def test_resend_verification_already_verified(test_app, test_client):
    """Test resend verification for already verified email."""
    # Setup mock database - verified user
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "id": "user123",
            "email": "test@example.com",
            "email_verified": True,  # Already verified
        }
    )
    test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = mock_conn
    test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = AsyncMock()

    response = await test_client.post(
        "/auth/resend-verification", json={"email": "test@example.com"}
    )

    assert response.status_code == 400
    assert "already verified" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_resend_verification_user_not_found(test_app, test_client):
    """Test resend verification for non-existent user."""
    # Setup mock database - user not found
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    test_app.state.services.db_logger.pool.acquire.return_value.__aenter__.return_value = mock_conn
    test_app.state.services.db_logger.pool.acquire.return_value.__aexit__.return_value = AsyncMock()

    response = await test_client.post(
        "/auth/resend-verification", json={"email": "nonexistent@example.com"}
    )

    assert response.status_code == 404
    assert "no account found" in response.json()["detail"].lower()
