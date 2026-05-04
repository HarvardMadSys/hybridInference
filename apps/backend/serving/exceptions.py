"""Custom exceptions for business logic.

This module defines domain-specific exceptions that represent business errors.
These exceptions are caught by global exception handlers and converted to
appropriate HTTP responses with consistent error codes.
"""


class HybridInferenceError(Exception):
    """Base exception for all business errors."""

    pass


class UserFacingError(HybridInferenceError):
    """Marker base for exceptions whose message is safe to surface verbatim.

    Subclasses' str(exc) is passed through scrub_error_for_user unchanged
    (with a request_id suffix appended).
    """

    pass


# Authentication errors
class AuthenticationError(UserFacingError):
    """Authentication related errors."""

    pass


class UserAlreadyExistsError(AuthenticationError):
    """User with email already exists."""

    def __init__(self, email: str):
        self.email = email
        super().__init__(f"Email {email} already registered")


class WeakPasswordError(AuthenticationError):
    """Password doesn't meet security requirements."""

    pass


class InvalidCredentialsError(AuthenticationError):
    """Invalid email or password."""

    pass


class EmailNotVerifiedError(AuthenticationError):
    """Email not verified."""

    pass


class AccountSuspendedError(AuthenticationError):
    """Account is suspended."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(f"Account is {status}")


# User management errors
class UserNotFoundError(UserFacingError):
    """User not found."""

    pass


class DuplicateAPIKeyError(UserFacingError):
    """User already has an active API key."""

    pass


class APIKeyNotFoundError(UserFacingError):
    """API key not found."""

    pass


# Token errors
class TokenExpiredError(AuthenticationError):
    """Token has expired."""

    pass


class InvalidTokenError(AuthenticationError):
    """Invalid token."""

    pass


class TokenAlreadyUsedError(AuthenticationError):
    """Token has already been used."""

    pass


# Session errors
class SessionNotFoundError(AuthenticationError):
    """Session not found."""

    pass


class SessionRevokedError(AuthenticationError):
    """Session has been revoked."""

    pass


# Quota errors
class QuotaExceededError(UserFacingError):
    """User has exceeded their quota."""

    def __init__(self, quota: float, spent: float):
        self.quota = quota
        self.spent = spent
        super().__init__(f"Quota exceeded: ${spent:.2f} / ${quota:.2f}")


# ----------------------------------------------------------------------
# User-facing error scrubbing
# ----------------------------------------------------------------------

_GENERIC_MESSAGES_BY_STATUS: dict[int, str] = {
    400: "Invalid request",
    401: "Authentication failed",
    403: "Authentication failed",
    422: "Invalid request",
    429: "Rate limit exceeded",
}


def scrub_error_for_user(
    exc: BaseException | None,
    request_id: str | None,
    status_code: int,
) -> str:
    """Return a user-safe error message containing no provider-specific info.

    The original exception text is intentionally NOT echoed back unless the
    exception is a `UserFacingError` subclass (whose message is, by author
    contract, free of provider info). Callers are responsible for persisting
    the full `str(exc)` to `api_logs.error` keyed by the same `request_id` in
    log stores that persist errors (Postgres, sqlite). The D1 buffered store
    has a slim schema that intentionally omits the `error` column, so in
    D1-only deployments the request_id surfaced to the user is not
    debuggable from D1 alone — operators must either run with Postgres /
    dual-write enabled or extend the D1 schema.
    """
    if isinstance(exc, UserFacingError):
        base = str(exc)
    elif status_code in _GENERIC_MESSAGES_BY_STATUS:
        base = _GENERIC_MESSAGES_BY_STATUS[status_code]
    elif 500 <= status_code < 600:
        base = "Internal server error"
    else:
        base = "Request failed"

    if request_id:
        return f"{base} (request_id: {request_id})"
    return base
