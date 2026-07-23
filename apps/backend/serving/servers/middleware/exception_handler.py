"""Global exception handlers for domain exceptions.

This module provides centralized exception handling that converts domain
exceptions into consistent HTTP responses with error codes.
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
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

_STABLE_CONTROL_PREFIXES = ("/admin/", "/auth/", "/control/v1/", "/user/")
_HTTP_ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "AUTHENTICATION_REQUIRED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "UPSTREAM_UNAVAILABLE",
    503: "SERVICE_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}


def is_stable_control_path(path: str) -> bool:
    """Return whether ``path`` belongs to the versioned control contract."""
    return path == "/capabilities" or path.startswith(_STABLE_CONTROL_PREFIXES)


def http_control_error_code(status_code: int) -> str:
    """Map an HTTP status to the stable control error vocabulary."""
    return _HTTP_ERROR_CODES.get(status_code, "HTTP_ERROR")


def _domain_error_payload(
    request: Request | Any,
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the stable control error envelope plus legacy top-level fields."""
    stable_details = details or {}
    payload: dict[str, Any] = {
        "error_code": code,
        "message": message,
        **stable_details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": {
            "code": code,
            "message": message,
            "details": stable_details,
        },
    }
    request_id = getattr(getattr(request, "state", None), "request_id", None)
    if request_id:
        payload["request_id"] = request_id
    return payload


def stable_control_http_exception_response(
    request: Request | Any,
    exc: Any,
) -> JSONResponse:
    """Normalize any HTTPException detail into the stable control envelope."""
    code = http_control_error_code(exc.status_code)
    message = str(exc.detail)
    details: dict[str, Any] = {}
    if isinstance(exc.detail, dict):
        inner = exc.detail.get("error")
        if isinstance(inner, dict):
            if isinstance(inner.get("code"), str) and inner["code"]:
                code = inner["code"]
            if isinstance(inner.get("message"), str) and inner["message"]:
                message = inner["message"]
            if isinstance(inner.get("details"), dict):
                details.update(inner["details"])
            details.update(
                {
                    key: value
                    for key, value in inner.items()
                    if key not in {"code", "details", "message", "type"}
                }
            )
        elif isinstance(inner, str):
            message = inner
        elif isinstance(exc.detail.get("message"), str):
            message = exc.detail["message"]
        details.update(
            {
                key: value
                for key, value in exc.detail.items()
                if key not in {"detail", "error", "error_code", "message", "timestamp"}
            }
        )
        legacy_code = exc.detail.get("error_code")
        if isinstance(legacy_code, str) and legacy_code:
            code = legacy_code

    return JSONResponse(
        status_code=exc.status_code,
        content=_domain_error_payload(
            request,
            code=code,
            message=message,
            details=details,
        ),
        headers=dict(exc.headers or {}),
    )


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
            content=_domain_error_payload(
                request,
                code="USER_ALREADY_EXISTS",
                message=str(exc),
                details={"email": exc.email},
            ),
        )

    @app.exception_handler(WeakPasswordError)
    async def weak_password_handler(request: Request, exc: WeakPasswordError):
        return JSONResponse(
            status_code=400,
            content=_domain_error_payload(request, code="WEAK_PASSWORD", message=str(exc)),
        )

    @app.exception_handler(InvalidCredentialsError)
    async def invalid_credentials_handler(request: Request, exc: InvalidCredentialsError):
        logger.warning(f"Invalid credentials attempt from {get_client_ip(request)}")
        return JSONResponse(
            status_code=401,
            content=_domain_error_payload(
                request,
                code="INVALID_CREDENTIALS",
                message="Invalid email or password",
            ),
        )

    @app.exception_handler(EmailNotVerifiedError)
    async def email_not_verified_handler(request: Request, exc: EmailNotVerifiedError):
        return JSONResponse(
            status_code=403,
            content=_domain_error_payload(request, code="EMAIL_NOT_VERIFIED", message=str(exc)),
        )

    @app.exception_handler(AccountSuspendedError)
    async def account_suspended_handler(request: Request, exc: AccountSuspendedError):
        return JSONResponse(
            status_code=403,
            content=_domain_error_payload(
                request,
                code="ACCOUNT_SUSPENDED",
                message=str(exc),
                details={"status": exc.status},
            ),
        )

    @app.exception_handler(UserNotFoundError)
    async def user_not_found_handler(request: Request, exc: UserNotFoundError):
        return JSONResponse(
            status_code=404,
            content=_domain_error_payload(request, code="USER_NOT_FOUND", message=str(exc)),
        )

    @app.exception_handler(DuplicateAPIKeyError)
    async def duplicate_key_handler(request: Request, exc: DuplicateAPIKeyError):
        return JSONResponse(
            status_code=409,
            content=_domain_error_payload(
                request,
                code="DUPLICATE_API_KEY",
                message=str(exc) or "You already have an active API key",
            ),
        )

    @app.exception_handler(APIKeyNotFoundError)
    async def key_not_found_handler(request: Request, exc: APIKeyNotFoundError):
        return JSONResponse(
            status_code=404,
            content=_domain_error_payload(
                request,
                code="API_KEY_NOT_FOUND",
                message=str(exc) or "No active API key found",
            ),
        )

    @app.exception_handler(TokenExpiredError)
    async def token_expired_handler(request: Request, exc: TokenExpiredError):
        return JSONResponse(
            status_code=401,
            content=_domain_error_payload(
                request,
                code="TOKEN_EXPIRED",
                message=str(exc) or "Token has expired",
            ),
        )

    @app.exception_handler(InvalidTokenError)
    async def invalid_token_handler(request: Request, exc: InvalidTokenError):
        return JSONResponse(
            status_code=401,
            content=_domain_error_payload(
                request,
                code="INVALID_TOKEN",
                message=str(exc) or "Invalid token",
            ),
        )

    @app.exception_handler(TokenAlreadyUsedError)
    async def token_used_handler(request: Request, exc: TokenAlreadyUsedError):
        return JSONResponse(
            status_code=400,
            content=_domain_error_payload(
                request,
                code="TOKEN_ALREADY_USED",
                message=str(exc) or "Token has already been used",
            ),
        )

    @app.exception_handler(SessionNotFoundError)
    async def session_not_found_handler(request: Request, exc: SessionNotFoundError):
        return JSONResponse(
            status_code=401,
            content=_domain_error_payload(
                request,
                code="SESSION_NOT_FOUND",
                message=str(exc) or "Session not found",
            ),
        )

    @app.exception_handler(SessionRevokedError)
    async def session_revoked_handler(request: Request, exc: SessionRevokedError):
        return JSONResponse(
            status_code=401,
            content=_domain_error_payload(
                request,
                code="SESSION_REVOKED",
                message=str(exc) or "Session has been revoked",
            ),
        )

    @app.exception_handler(QuotaExceededError)
    async def quota_exceeded_handler(request: Request, exc: QuotaExceededError):
        return JSONResponse(
            status_code=429,
            content=_domain_error_payload(
                request,
                code="QUOTA_EXCEEDED",
                message=str(exc),
                details={"quota": exc.quota, "spent": exc.spent},
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError):
        if not is_stable_control_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        issues = [
            {
                "location": [str(part) for part in issue.get("loc", ())],
                "type": str(issue.get("type", "validation_error")),
                "message": str(issue.get("msg", "Invalid request")),
            }
            for issue in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_domain_error_payload(
                request,
                code="VALIDATION_ERROR",
                message="Request validation failed",
                details={"issues": issues},
            ),
        )

    logger.info("Exception handlers installed")
