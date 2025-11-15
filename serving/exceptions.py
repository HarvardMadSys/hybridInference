"""Custom exceptions for business logic.

This module defines domain-specific exceptions that represent business errors.
These exceptions are caught by global exception handlers and converted to
appropriate HTTP responses with consistent error codes.
"""


class HybridInferenceError(Exception):
    """Base exception for all business errors."""

    pass


# Authentication errors
class AuthenticationError(HybridInferenceError):
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
class UserNotFoundError(HybridInferenceError):
    """User not found."""

    pass


class DuplicateAPIKeyError(HybridInferenceError):
    """User already has an active API key."""

    pass


class APIKeyNotFoundError(HybridInferenceError):
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
class QuotaExceededError(HybridInferenceError):
    """User has exceeded their quota."""

    def __init__(self, quota: float, spent: float):
        self.quota = quota
        self.spent = spent
        super().__init__(f"Quota exceeded: ${spent:.2f} / ${quota:.2f}")
