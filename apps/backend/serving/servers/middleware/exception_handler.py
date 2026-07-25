"""Global exception handlers for domain exceptions.

This module provides centralized exception handling that converts domain
exceptions into consistent HTTP responses with error codes.
"""

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from serving.exceptions import (
    AccountSuspendedError,
    APIKeyNotFoundError,
    DuplicateAPIKeyError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
    InvalidTokenError,
    QuotaExceededError,
    SessionNotFoundError,
    SessionRevokedError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    UserAlreadyExistsError,
    UserNotFoundError,
    WeakPasswordError,
)
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

logger = get_logger(__name__)


def install_exception_handlers(app: FastAPI) -> None:
    """Install all exception handlers on the FastAPI app.

    Args:
        app: FastAPI application instance.
    """

    @app.exception_handler(UserAlreadyExistsError)
    async def user_exists_handler(request: Request, exc: UserAlreadyExistsError):
        logger.warning(f"User already exists: {exc.email}")
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "USER_ALREADY_EXISTS",
                "message": str(exc),
                "email": exc.email,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(WeakPasswordError)
    async def weak_password_handler(request: Request, exc: WeakPasswordError):
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "WEAK_PASSWORD",
                "message": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(InvalidCredentialsError)
    async def invalid_credentials_handler(request: Request, exc: InvalidCredentialsError):
        logger.warning(f"Invalid credentials attempt from {get_client_ip(request)}")
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "INVALID_CREDENTIALS",
                "message": "Invalid email or password",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(EmailNotVerifiedError)
    async def email_not_verified_handler(request: Request, exc: EmailNotVerifiedError):
        return JSONResponse(
            status_code=403,
            content={
                "error_code": "EMAIL_NOT_VERIFIED",
                "message": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(AccountSuspendedError)
    async def account_suspended_handler(request: Request, exc: AccountSuspendedError):
        return JSONResponse(
            status_code=403,
            content={
                "error_code": "ACCOUNT_SUSPENDED",
                "message": str(exc),
                "status": exc.status,
                # Optional admin-authored message shown to the user; null when unset.
                "suspension_message": exc.suspension_message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(UserNotFoundError)
    async def user_not_found_handler(request: Request, exc: UserNotFoundError):
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "USER_NOT_FOUND",
                "message": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(DuplicateAPIKeyError)
    async def duplicate_key_handler(request: Request, exc: DuplicateAPIKeyError):
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "DUPLICATE_API_KEY",
                "message": str(exc) or "You already have an active API key",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(APIKeyNotFoundError)
    async def key_not_found_handler(request: Request, exc: APIKeyNotFoundError):
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "API_KEY_NOT_FOUND",
                "message": str(exc) or "No active API key found",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(TokenExpiredError)
    async def token_expired_handler(request: Request, exc: TokenExpiredError):
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "TOKEN_EXPIRED",
                "message": str(exc) or "Token has expired",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(InvalidTokenError)
    async def invalid_token_handler(request: Request, exc: InvalidTokenError):
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "INVALID_TOKEN",
                "message": str(exc) or "Invalid token",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(TokenAlreadyUsedError)
    async def token_used_handler(request: Request, exc: TokenAlreadyUsedError):
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "TOKEN_ALREADY_USED",
                "message": str(exc) or "Token has already been used",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(SessionNotFoundError)
    async def session_not_found_handler(request: Request, exc: SessionNotFoundError):
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "SESSION_NOT_FOUND",
                "message": str(exc) or "Session not found",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(SessionRevokedError)
    async def session_revoked_handler(request: Request, exc: SessionRevokedError):
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "SESSION_REVOKED",
                "message": str(exc) or "Session has been revoked",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(QuotaExceededError)
    async def quota_exceeded_handler(request: Request, exc: QuotaExceededError):
        return JSONResponse(
            status_code=429,
            content={
                "error_code": "QUOTA_EXCEEDED",
                "message": str(exc),
                "quota": exc.quota,
                "spent": exc.spent,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    logger.info("Exception handlers installed")
