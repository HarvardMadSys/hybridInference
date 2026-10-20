"""Unit tests for exception handlers."""

from datetime import datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.exceptions import (
    DuplicateAPIKeyError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
    QuotaExceededError,
    UserAlreadyExistsError,
)
from serving.servers.middleware.exception_handler import install_exception_handlers


@pytest.fixture
def app_with_handlers():
    """Create FastAPI app with exception handlers installed."""
    app = FastAPI()

    # Add test routes that raise exceptions
    @app.get("/test/user-exists")
    def raise_user_exists():
        raise UserAlreadyExistsError("test@example.com")

    @app.get("/test/invalid-credentials")
    def raise_invalid_credentials():
        raise InvalidCredentialsError()

    @app.get("/test/duplicate-key")
    def raise_duplicate_key():
        raise DuplicateAPIKeyError("Key already exists")

    @app.get("/test/quota-exceeded")
    def raise_quota_exceeded():
        raise QuotaExceededError(quota=100.0, spent=105.5)

    @app.get("/test/email-not-verified")
    def raise_email_not_verified():
        raise EmailNotVerifiedError()

    # Install exception handlers
    install_exception_handlers(app)

    return app


class TestExceptionHandlers:
    """Test exception handlers return consistent responses."""

    @pytest.mark.asyncio
    async def test_user_already_exists_handler(self, app_with_handlers):
        """Test UserAlreadyExistsError returns 409 without echoing the address.

        This used to assert the opposite -- that the submitted address came back
        in ``message`` and again in an ``email`` field. That is a membership
        oracle: anyone with a list of addresses could sort it into "registered
        here" and "not" by reading the error. The 409 and the ``error_code``
        stay (the console's error map keys off the code, and the status is the
        client contract); the address does not.
        """
        transport = ASGITransport(app=app_with_handlers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/test/user-exists")

            assert response.status_code == 409
            data = response.json()
            assert data["error_code"] == "USER_ALREADY_EXISTS"
            assert "test@example.com" not in response.text
            assert "email" not in data
            assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_invalid_credentials_handler(self, app_with_handlers):
        """Test InvalidCredentialsError returns 401 with correct format."""
        transport = ASGITransport(app=app_with_handlers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/test/invalid-credentials")

            assert response.status_code == 401
            data = response.json()
            assert data["error_code"] == "INVALID_CREDENTIALS"
            assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_raised_message_does_not_reach_the_body(self, app_with_handlers):
        """Whatever a raiser interpolates stays server-side.

        ``DuplicateAPIKeyError("Key already exists")`` is benign, but the same
        handler shape is what carried the internal user id out of
        ``UserNotFoundError(current_user["user_id"])``. The handler now writes
        its own static message rather than ``str(exc)``, so the raiser's text
        cannot reach a client regardless of what it says.
        """
        transport = ASGITransport(app=app_with_handlers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/test/duplicate-key")

            assert response.status_code == 409
            assert "Key already exists" not in response.text
            assert response.json()["message"] == "You already have an active API key"

    @pytest.mark.asyncio
    async def test_duplicate_api_key_handler(self, app_with_handlers):
        """Test DuplicateAPIKeyError returns 409 with correct format."""
        transport = ASGITransport(app=app_with_handlers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/test/duplicate-key")

            assert response.status_code == 409
            data = response.json()
            assert data["error_code"] == "DUPLICATE_API_KEY"
            assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_quota_exceeded_handler(self, app_with_handlers):
        """Test QuotaExceededError returns 429 with quota details."""
        transport = ASGITransport(app=app_with_handlers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/test/quota-exceeded")

            assert response.status_code == 429
            data = response.json()
            assert data["error_code"] == "QUOTA_EXCEEDED"
            # Kept, deliberately: a caller's own spend against their own cap is
            # theirs to read, and it is what tells them how long to wait.
            assert data["quota"] == 100.0
            assert data["spent"] == 105.5
            assert data["message"] == "Quota exceeded: $105.50 / $100.00"
            assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_email_not_verified_handler(self, app_with_handlers):
        """Test EmailNotVerifiedError returns 403 with correct format."""
        transport = ASGITransport(app=app_with_handlers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/test/email-not-verified")

            assert response.status_code == 403
            data = response.json()
            assert data["error_code"] == "EMAIL_NOT_VERIFIED"
            assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_error_response_format_consistency(self, app_with_handlers):
        """Test all error responses have consistent format."""
        transport = ASGITransport(app=app_with_handlers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            endpoints = [
                "/test/user-exists",
                "/test/invalid-credentials",
                "/test/duplicate-key",
                "/test/quota-exceeded",
                "/test/email-not-verified",
            ]

            for endpoint in endpoints:
                response = await client.get(endpoint)
                data = response.json()

                # All responses should have these fields
                assert "error_code" in data
                assert "message" in data
                assert "timestamp" in data

                # Timestamp should be valid ISO format
                datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))


# --- the token, session and key failures ---------------------------------------

from serving.exceptions import (
    APIKeyNotFoundError,
    InvalidTokenError,
    SessionNotFoundError,
    SessionRevokedError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    WeakPasswordError,
)

_RAISED_TEXT = "refresh token tok_live_abc123 for u-42 at db-internal:5432"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_type", "status", "code", "message"),
    [
        (WeakPasswordError, 400, "WEAK_PASSWORD", "Password does not meet security requirements"),
        (APIKeyNotFoundError, 404, "API_KEY_NOT_FOUND", "No active API key found"),
        (TokenExpiredError, 401, "TOKEN_EXPIRED", "Token has expired"),
        (InvalidTokenError, 401, "INVALID_TOKEN", "Invalid token"),
        (TokenAlreadyUsedError, 400, "TOKEN_ALREADY_USED", "Token has already been used"),
        (SessionNotFoundError, 401, "SESSION_NOT_FOUND", "Session not found"),
        (SessionRevokedError, 401, "SESSION_REVOKED", "Session has been revoked"),
    ],
)
async def test_a_token_or_session_failure_answers_with_its_code_never_its_text(
    exc_type, status, code, message
):
    """The raised text may name a token, a user or a host; the client gets the code."""
    app = FastAPI()

    @app.get("/raise")
    def raise_it():
        raise exc_type(_RAISED_TEXT)

    install_exception_handlers(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/raise")

    body = response.json()
    assert response.status_code == status
    assert (body["error_code"], body["message"]) == (code, message)
    assert datetime.fromisoformat(body["timestamp"])
    assert "tok_live_abc123" not in response.text and "db-internal" not in response.text
