"""Global exception handlers for domain exceptions.

This module provides centralized exception handling that converts domain
exceptions into consistent HTTP responses with error codes.

**Response detail is redacted; the log is not.** Every handler below used to
put ``str(exc)`` in the ``message`` field, which is the one piece of a domain
exception that carries whatever the raiser happened to interpolate -- an email
address on the signup 409 (a user-enumeration oracle), the internal user id on
the 404 (``UserNotFoundError(current_user["user_id"])`` in user_routes.py), a
password-policy string, or anything a distribution's extension chose to raise
with. Each handler now emits its own developer-authored static message instead,
and :func:`_log_domain_exception` writes the full exception text to the server
log keyed by the request path, so support can still see what actually happened.

What deliberately survives the redaction, because removing it would break a
legitimate client rather than protect anyone:

* the **HTTP status code** of every handler, unchanged (429 stays 429, 403
  stays 403). Clients back off on the status, not on the prose;
* the machine-readable ``error_code``, which is the contract the console's
  error mapper (``apps/frontend/src/lib/utils/errors.ts``) keys off;
* the user's **own** quota numbers on the 429 -- their spend and their cap are
  their data, not ours;
* the admin-authored ``suspension_message``, which exists precisely to be shown
  to the suspended user.
"""

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from serving.exceptions import (
    AccountSuspendedError,
    APIKeyNotFoundError,
    DuplicateAPIKeyError,
    EmailNotVerifiedError,
    HybridInferenceError,
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


def _log_domain_exception(
    request: Request, exc: HybridInferenceError, error_code: str, status_code: int
) -> None:
    """Record the full exception text server-side.

    The response body no longer carries ``str(exc)``, so this is the only place
    the raiser's own message survives. Keyed by path and status so a support
    request quoting the ``X-Request-ID`` header (attached to every response by
    ``RequestIdMiddleware``) can be matched to the line that explains it.

    ``detail`` and ``error_code`` must stay in
    ``serving.utils.logging._STRUCTURED_LOG_KEYS``: both formatters emit only
    the keys listed there, so dropping either would delete this line's payload
    at format time -- ``detail`` is the text the response stopped carrying, and
    ``error_code`` is the only machine-readable field here -- and the redaction
    would become a net loss of evidence rather than a relocation of it.
    """
    logger.warning(
        "domain_error",
        extra={
            "error_code": error_code,
            "status_code": status_code,
            "path": request.url.path,
            "method": request.method,
            "detail": str(exc),
        },
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
            content={
                "error_code": "USER_ALREADY_EXISTS",
                # Neither the message nor a separate ``email`` field echoes the
                # submitted address back. The address is the signup form's own
                # input, but echoing it on an error turns the endpoint into a
                # membership oracle for anyone with a list of addresses to try.
                # Matches the wording the hardened signup path in auth_routes.py
                # already returns for the same condition.
                "message": "Registration failed. Please try again.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(WeakPasswordError)
    async def weak_password_handler(request: Request, exc: WeakPasswordError):
        # The specific policy hint ("must be at least N characters") is not
        # lost to users: auth_routes.py surfaces it as a plain
        # HTTPException(400, error_msg), a developer-authored contract message
        # that the HTTP envelope keeps. This handler covers exceptions raised
        # from elsewhere, where the text is whatever the raiser chose.
        _log_domain_exception(request, exc, "WEAK_PASSWORD", 400)
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "WEAK_PASSWORD",
                "message": "Password does not meet security requirements",
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
        _log_domain_exception(request, exc, "EMAIL_NOT_VERIFIED", 403)
        return JSONResponse(
            status_code=403,
            content={
                "error_code": "EMAIL_NOT_VERIFIED",
                "message": "Email not verified. Please check your email for the verification link.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(AccountSuspendedError)
    async def account_suspended_handler(request: Request, exc: AccountSuspendedError):
        _log_domain_exception(request, exc, "ACCOUNT_SUSPENDED", 403)
        return JSONResponse(
            status_code=403,
            content={
                "error_code": "ACCOUNT_SUSPENDED",
                # Rendered here from the typed field rather than taken from
                # str(exc): same text, but the shape is ours and cannot pick up
                # whatever a future raiser appends to the message.
                "message": f"Account is {exc.status}",
                "status": exc.status,
                # Optional admin-authored message shown to the user; null when
                # unset. Kept: it is written by an operator *for* this user, not
                # internal detail that leaked into an exception.
                "suspension_message": exc.suspension_message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(UserNotFoundError)
    async def user_not_found_handler(request: Request, exc: UserNotFoundError):
        # str(exc) here was the internal user id: user_routes.py raises
        # UserNotFoundError(current_user["user_id"]).
        _log_domain_exception(request, exc, "USER_NOT_FOUND", 404)
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "USER_NOT_FOUND",
                "message": "User not found",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(DuplicateAPIKeyError)
    async def duplicate_key_handler(request: Request, exc: DuplicateAPIKeyError):
        _log_domain_exception(request, exc, "DUPLICATE_API_KEY", 409)
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "DUPLICATE_API_KEY",
                "message": "You already have an active API key",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(APIKeyNotFoundError)
    async def key_not_found_handler(request: Request, exc: APIKeyNotFoundError):
        _log_domain_exception(request, exc, "API_KEY_NOT_FOUND", 404)
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "API_KEY_NOT_FOUND",
                "message": "No active API key found",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(TokenExpiredError)
    async def token_expired_handler(request: Request, exc: TokenExpiredError):
        _log_domain_exception(request, exc, "TOKEN_EXPIRED", 401)
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "TOKEN_EXPIRED",
                "message": "Token has expired",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(InvalidTokenError)
    async def invalid_token_handler(request: Request, exc: InvalidTokenError):
        _log_domain_exception(request, exc, "INVALID_TOKEN", 401)
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "INVALID_TOKEN",
                "message": "Invalid token",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(TokenAlreadyUsedError)
    async def token_used_handler(request: Request, exc: TokenAlreadyUsedError):
        _log_domain_exception(request, exc, "TOKEN_ALREADY_USED", 400)
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "TOKEN_ALREADY_USED",
                "message": "Token has already been used",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(SessionNotFoundError)
    async def session_not_found_handler(request: Request, exc: SessionNotFoundError):
        _log_domain_exception(request, exc, "SESSION_NOT_FOUND", 401)
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "SESSION_NOT_FOUND",
                "message": "Session not found",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(SessionRevokedError)
    async def session_revoked_handler(request: Request, exc: SessionRevokedError):
        _log_domain_exception(request, exc, "SESSION_REVOKED", 401)
        return JSONResponse(
            status_code=401,
            content={
                "error_code": "SESSION_REVOKED",
                "message": "Session has been revoked",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.exception_handler(QuotaExceededError)
    async def quota_exceeded_handler(request: Request, exc: QuotaExceededError):
        # The fourth 429 quota shape in this codebase (the other three: the
        # hand-rolled body in servers/auth.py, quota.exceeded_payload used by
        # the grant door, and the concurrency limiter). Nothing here is
        # redacted: the numbers are the caller's own spend against their own
        # cap, which is exactly what a client needs to decide how long to wait,
        # and the message is rendered from those typed fields rather than taken
        # from str(exc).
        _log_domain_exception(request, exc, "QUOTA_EXCEEDED", 429)
        return JSONResponse(
            status_code=429,
            content={
                "error_code": "QUOTA_EXCEEDED",
                "message": f"Quota exceeded: ${exc.spent:.2f} / ${exc.quota:.2f}",
                "quota": exc.quota,
                "spent": exc.spent,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    logger.info("Exception handlers installed")
