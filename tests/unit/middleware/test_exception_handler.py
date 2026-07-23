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
from serving.servers.middleware.request_id import RequestIdMiddleware


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
    app.add_middleware(RequestIdMiddleware)

    return app


class TestExceptionHandlers:
    """Test exception handlers return consistent responses."""

    @pytest.mark.asyncio
    async def test_user_already_exists_handler(self, app_with_handlers):
        """Test UserAlreadyExistsError returns 409 with correct format."""
        transport = ASGITransport(app=app_with_handlers)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/test/user-exists")

            assert response.status_code == 409
            data = response.json()
            assert data["error_code"] == "USER_ALREADY_EXISTS"
            assert "test@example.com" in data["message"]
            assert "email" in data
            assert data["email"] == "test@example.com"
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
            assert data["quota"] == 100.0
            assert data["spent"] == 105.5
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
                assert data["error"] == {
                    "code": data["error_code"],
                    "message": data["message"],
                    "details": {
                        key: data[key]
                        for key in ("email", "status", "quota", "spent")
                        if key in data
                    },
                }
                assert data["request_id"] == response.headers["x-request-id"]

                # Timestamp should be valid ISO format
                datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
