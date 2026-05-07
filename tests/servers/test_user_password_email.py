"""Tests for user password and email change functionality."""

from unittest.mock import patch

import pytest

# Real argon2 hash for "OldPassword123" - used in mocks
MOCK_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$somebase64salt$somebase64hash"


def _mock_authenticated_user(email: str) -> dict[str, str | bool]:
    """Build a current-user row that matches get_current_user() expectations."""
    return {
        "id": "user123",
        "email": email,
        "status": "active",
        "email_verified": True,
        "role": "free",
        "password_hash": MOCK_PASSWORD_HASH,
        "user_name": "Test User",
        "created_at": "2025-01-01T00:00:00+00:00",
        "last_login_at": None,
    }


@pytest.mark.asyncio
async def test_change_password_success(test_app, test_client, mock_operational_store):
    """Test successful password change."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", return_value=True),
    ):
        mock_verify.return_value = {"sub": "user123", "email": "test@example.com"}

        mock_operational_store.get_user_by_id.return_value = _mock_authenticated_user(
            "test@example.com"
        )
        mock_operational_store.update_user_fields.return_value = None

        response = await test_client.post(
            "/user/change-password",
            json={"old_password": "OldPassword123", "new_password": "NewPassword456"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 200
    data = response.json()
    assert "password changed successfully" in data["message"].lower()


@pytest.mark.asyncio
async def test_change_password_wrong_old_password(test_app, test_client, mock_operational_store):
    """Test password change with incorrect old password."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", return_value=False),
    ):
        mock_verify.return_value = {"sub": "user123", "email": "test@example.com"}

        mock_operational_store.get_user_by_id.return_value = _mock_authenticated_user(
            "test@example.com"
        )

        response = await test_client.post(
            "/user/change-password",
            json={"old_password": "WrongPassword123", "new_password": "NewPassword456"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 400
    assert "incorrect" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_password_same_as_old(test_app, test_client, mock_operational_store):
    """Test password change with new password same as old."""
    with (
        patch("serving.utils.jwt.verify_access_token") as mock_verify,
        patch("serving.utils.password.verify_password", side_effect=[True, True]),
    ):
        mock_verify.return_value = {"sub": "user123", "email": "test@example.com"}

        mock_operational_store.get_user_by_id.return_value = _mock_authenticated_user(
            "test@example.com"
        )

        response = await test_client.post(
            "/user/change-password",
            json={"old_password": "SamePassword123", "new_password": "SamePassword123"},
            headers={"Authorization": "Bearer fake_token"},
        )

    assert response.status_code == 400
    assert "different" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_password_weak_new_password(test_app, test_client, mock_operational_store):
    """Test password change with weak new password."""
    with patch("serving.utils.jwt.verify_access_token") as mock_verify:
        mock_verify.return_value = {"sub": "user123", "email": "test@example.com"}

        mock_operational_store.get_user_by_id.return_value = _mock_authenticated_user(
            "test@example.com"
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
async def test_change_password_requires_auth(test_client):
    """Test that change password requires authentication."""
    response = await test_client.post(
        "/user/change-password",
        json={"old_password": "OldPassword123", "new_password": "NewPassword456"},
    )
    assert response.status_code == 401
