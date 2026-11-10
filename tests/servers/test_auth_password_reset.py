"""Tests for password reset functionality."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient


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
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
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
async def test_reset_password_retires_every_outstanding_link(
    test_app, test_client, mock_operational_store
):
    """Every link the account still holds was sent to reset the password just replaced."""
    mock_operational_store.get_reset_token.return_value = {
        "user_id": "user123",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "used_at": None,
    }

    response = await test_client.post(
        "/auth/reset-password", json={"token": "valid_token_123", "new_password": "NewPassword123"}
    )

    assert response.status_code == 200
    mock_operational_store.delete_user_reset_tokens.assert_awaited_once_with("user123")


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
        "expires_at": datetime.now(UTC) - timedelta(hours=1),  # Expired
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
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "used_at": datetime.now(UTC) - timedelta(minutes=10),  # Already used
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


# -- mail floods (#16) ---------------------------------------------------------

_LIMITED = {"detail": "Too many requests. Please try again later."}


def _account(**overrides):
    return {"id": "user123", "email": "test@example.com", "email_verified": False, **overrides}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "kind"),
    [("/auth/forgot-password", "reset"), ("/auth/resend-verification", "verification")],
)
async def test_mail_routes_refuse_an_address_alike_whether_or_not_it_has_an_account(
    test_app, test_client, mock_operational_store, path, kind
):
    """Three an hour per address, then the same 429 for a held and a free one."""
    mock_operational_store.get_live_reset_token.return_value = "live"
    mock_operational_store.get_live_verification_token.return_value = "live"
    answers = {}
    for email, account in (
        ("held@example.com", _account(email="held@example.com")),
        ("free@example.com", None),
    ):
        mock_operational_store.get_user_by_email.return_value = account
        for _ in range(3):
            response = await test_client.post(path, json={"email": email})
            assert response.status_code == 200
        lookups = mock_operational_store.get_user_by_email.await_count
        refused = await test_client.post(path, json={"email": email.upper()})
        answers[email] = (refused.status_code, refused.json(), refused.headers["Retry-After"])
        # Refused before the account is looked up.
        assert mock_operational_store.get_user_by_email.await_count == lookups

    assert answers["held@example.com"] == answers["free@example.com"] == (429, _LIMITED, "3600")


def _client_at(app, address: str) -> AsyncClient:
    transport = ASGITransport(app=app, client=(address, 123))
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_mail_routes_share_a_window_per_client(test_app, mock_operational_store):
    mock_operational_store.get_user_by_email.return_value = None
    async with _client_at(test_app, "5.6.7.8") as client:
        for n in range(10):
            path = "/auth/forgot-password" if n % 2 else "/auth/resend-verification"
            response = await client.post(path, json={"email": f"user{n}@example.com"})
            assert response.status_code == 200

        refused = await client.post("/auth/forgot-password", json={"email": "new@example.com"})

    assert (refused.status_code, refused.json()) == (429, _LIMITED)


@pytest.mark.asyncio
async def test_clients_the_backend_cannot_tell_apart_do_not_share_one_window(
    test_app, test_client, mock_operational_store
):
    """Loopback is unresolved, as is every browser behind an untrusted proxy.

    One window for all of them was a cap on the deployment: ten requests an
    hour from anyone stopped every password reset.
    """
    mock_operational_store.get_user_by_email.return_value = None
    for n in range(30):
        response = await test_client.post("/auth/forgot-password", json={"email": f"x{n}@e.com"})
        assert response.status_code == 200

    owner = await test_client.post("/auth/forgot-password", json={"email": "owner@example.com"})
    assert owner.status_code == 200


@pytest.mark.asyncio
async def test_forgot_password_sends_a_live_link_again_instead_of_minting_one(
    test_app, test_client, mock_operational_store, monkeypatch
):
    sent = []
    monkeypatch.setattr("serving.servers.routers.auth_routes.is_email_enabled", lambda: True)
    monkeypatch.setattr(
        "serving.utils.email.send_password_reset_email",
        lambda email, token, base_url: sent.append((email, token)),
    )
    mock_operational_store.get_user_by_email.return_value = _account()
    mock_operational_store.get_live_reset_token.return_value = "still-good"

    before = datetime.now(UTC)
    response = await test_client.post("/auth/forgot-password", json={"email": "test@example.com"})

    assert response.status_code == 200
    mock_operational_store.create_reset_token.assert_not_awaited()
    assert sent == [("test@example.com", "still-good")]
    call = mock_operational_store.get_live_reset_token.await_args
    assert call.args == ("user123",)
    # Only a link with at least half its hour left is sent again.
    valid_until = call.kwargs["valid_until"]
    assert (
        before + timedelta(minutes=30) <= valid_until <= datetime.now(UTC) + timedelta(minutes=30)
    )


@pytest.mark.asyncio
async def test_forgot_password_mints_a_link_when_none_is_live(
    test_app, test_client, mock_operational_store, monkeypatch
):
    sent = []
    monkeypatch.setattr("serving.servers.routers.auth_routes.is_email_enabled", lambda: True)
    monkeypatch.setattr(
        "serving.utils.email.send_password_reset_email",
        lambda email, token, base_url: sent.append((email, token)),
    )
    mock_operational_store.get_user_by_email.return_value = _account()
    mock_operational_store.get_live_reset_token.return_value = None

    response = await test_client.post("/auth/forgot-password", json={"email": "test@example.com"})

    assert response.status_code == 200
    created = mock_operational_store.create_reset_token.await_args.kwargs
    assert created["user_id"] == "user123"
    assert created["expires_at"] - datetime.now(UTC) > timedelta(minutes=59)
    assert sent == [("test@example.com", created["token"])]


@pytest.mark.asyncio
async def test_resend_verification_sends_a_live_link_again_instead_of_minting_one(
    test_app, test_client, mock_operational_store, monkeypatch
):
    sent = []
    monkeypatch.setattr("serving.servers.routers.auth_routes.is_email_enabled", lambda: True)
    monkeypatch.setattr(
        "serving.servers.routers.auth_routes.send_verification_email",
        lambda email, token, base_url: sent.append((email, token)),
    )
    mock_operational_store.get_user_by_email.return_value = _account()
    mock_operational_store.get_live_verification_token.return_value = "still-good"

    before = datetime.now(UTC)
    response = await test_client.post(
        "/auth/resend-verification", json={"email": "test@example.com"}
    )

    assert response.status_code == 200
    mock_operational_store.create_verification_token.assert_not_awaited()
    assert sent == [("test@example.com", "still-good")]
    valid_until = mock_operational_store.get_live_verification_token.await_args.kwargs[
        "valid_until"
    ]
    assert before + timedelta(hours=12) <= valid_until <= datetime.now(UTC) + timedelta(hours=12)


@pytest.mark.asyncio
async def test_resend_verification_mints_a_link_when_none_is_live(
    test_app, test_client, mock_operational_store, monkeypatch
):
    sent = []
    monkeypatch.setattr("serving.servers.routers.auth_routes.is_email_enabled", lambda: True)
    monkeypatch.setattr(
        "serving.servers.routers.auth_routes.send_verification_email",
        lambda email, token, base_url: sent.append((email, token)),
    )
    mock_operational_store.get_user_by_email.return_value = _account()
    mock_operational_store.get_live_verification_token.return_value = None

    response = await test_client.post(
        "/auth/resend-verification", json={"email": "test@example.com"}
    )

    assert response.status_code == 200
    created = mock_operational_store.create_verification_token.await_args.kwargs
    assert created["expires_at"] - datetime.now(UTC) > timedelta(hours=23)
    assert sent == [("test@example.com", created["token"])]
