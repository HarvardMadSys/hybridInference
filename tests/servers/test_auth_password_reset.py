"""Tests for password reset functionality."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_forgot_password_success(test_app, test_client, mock_operational_store):
    """Test successful password reset request."""
    with (
        patch("serving.utils.email.is_email_enabled", return_value=True),
        patch("serving.utils.email.send_password_reset_email", return_value=True),
    ):
        mock_operational_store.get_user_by_email.return_value = {
            "id": "user123",
            "email": "test@example.com",
        }
        mock_operational_store.create_reset_token.return_value = None

        response = await test_client.post(
            "/auth/forgot-password", json={"email": "test@example.com"}
        )

    assert response.status_code == 200
    data = response.json()
    assert "password reset link has been sent" in data["message"].lower()


@pytest.mark.asyncio
async def test_forgot_password_nonexistent_email(test_app, test_client, mock_operational_store):
    """Test password reset for non-existent email (should still return success)."""
    mock_operational_store.get_user_by_email.return_value = None

    response = await test_client.post(
        "/auth/forgot-password", json={"email": "nonexistent@example.com"}
    )

    # Should still return success to prevent email enumeration
    assert response.status_code == 200
    data = response.json()
    assert "password reset link has been sent" in data["message"].lower()


@pytest.mark.asyncio
async def test_reset_password_success(test_app, test_client, mock_operational_store):
    """Test successful password reset with valid token."""
    mock_operational_store.get_reset_token.return_value = {
        "user_id": "user123",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "used_at": None,
    }
    mock_operational_store.update_user_fields.return_value = None
    mock_operational_store.mark_reset_used.return_value = None
    mock_operational_store.delete_user_sessions.return_value = None

    response = await test_client.post(
        "/auth/reset-password", json={"token": "valid_token_123", "new_password": "NewPassword123"}
    )

    assert response.status_code == 200
    data = response.json()
    assert "password has been reset" in data["message"].lower()


@pytest.mark.asyncio
async def test_reset_password_invalid_token(test_app, test_client, mock_operational_store):
    """Test password reset with invalid token."""
    mock_operational_store.get_reset_token.return_value = None

    response = await test_client.post(
        "/auth/reset-password", json={"token": "invalid_token", "new_password": "NewPassword123"}
    )

    assert response.status_code == 400
    assert "invalid" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reset_password_expired_token(test_app, test_client, mock_operational_store):
    """Test password reset with expired token."""
    mock_operational_store.get_reset_token.return_value = {
        "user_id": "user123",
        "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),  # Expired
        "used_at": None,
    }

    response = await test_client.post(
        "/auth/reset-password", json={"token": "expired_token", "new_password": "NewPassword123"}
    )

    assert response.status_code == 400
    assert "expired" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reset_password_already_used_token(test_app, test_client, mock_operational_store):
    """Test password reset with already used token."""
    mock_operational_store.get_reset_token.return_value = {
        "user_id": "user123",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "used_at": datetime.now(timezone.utc) - timedelta(minutes=10),  # Already used
    }

    response = await test_client.post(
        "/auth/reset-password", json={"token": "used_token", "new_password": "NewPassword123"}
    )

    assert response.status_code == 400
    assert "already been used" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reset_password_weak_password(test_app, test_client, mock_operational_store):
    """Test password reset with weak password."""
    # The password validation happens before token lookup in reset_password
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
async def test_resend_verification_success(test_app, test_client, mock_operational_store):
    """Test successful resend of verification email."""
    with (
        patch("serving.utils.email.is_email_enabled", return_value=True),
        patch("serving.utils.email.send_verification_email", return_value=True),
    ):
        mock_operational_store.get_user_by_email.return_value = {
            "id": "user123",
            "email": "test@example.com",
            "email_verified": False,
        }
        mock_operational_store.create_verification_token.return_value = None

        response = await test_client.post(
            "/auth/resend-verification", json={"email": "test@example.com"}
        )

    assert response.status_code == 200
    data = response.json()
    assert "verification email has been sent" in data["message"].lower()


@pytest.mark.asyncio
async def test_resend_verification_already_verified(test_app, test_client, mock_operational_store):
    """Test resend verification for already verified email."""
    mock_operational_store.get_user_by_email.return_value = {
        "id": "user123",
        "email": "test@example.com",
        "email_verified": True,  # Already verified
    }

    response = await test_client.post(
        "/auth/resend-verification", json={"email": "test@example.com"}
    )

    assert response.status_code == 200
    assert "verification email has been sent" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_resend_verification_user_not_found(test_app, test_client, mock_operational_store):
    """Test resend verification for non-existent user."""
    mock_operational_store.get_user_by_email.return_value = None

    response = await test_client.post(
        "/auth/resend-verification", json={"email": "nonexistent@example.com"}
    )

    assert response.status_code == 200
    assert "verification email has been sent" in response.json()["message"].lower()
