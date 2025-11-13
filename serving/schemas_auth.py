"""Pydantic schemas for authentication and user management."""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


# Authentication request/response schemas
class SignupRequest(BaseModel):
    """User signup request."""

    email: EmailStr
    password: str = Field(..., min_length=8)
    user_name: str | None = None


class SignupResponse(BaseModel):
    """User signup response."""

    message: str
    email: str
    user_id: str


class LoginRequest(BaseModel):
    """User login request."""

    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    """User login response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserInfo"


class RefreshResponse(BaseModel):
    """Token refresh response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LogoutResponse(BaseModel):
    """Logout response."""

    message: str


class VerifyEmailResponse(BaseModel):
    """Email verification response."""

    message: str
    email_verified: bool


# User info schemas
class UserInfo(BaseModel):
    """User information (public)."""

    id: str
    email: str
    user_name: str | None = None
    tier: str = "free"
    status: str = "active"
    email_verified: bool = False
    created_at: datetime
    last_login_at: datetime | None = None


class UserProfileUpdate(BaseModel):
    """User profile update request."""

    user_name: str | None = None


# API key schemas
class APIKeyCreate(BaseModel):
    """API key creation request (no body needed)."""

    pass


class APIKeyResponse(BaseModel):
    """API key creation response (full key shown only once)."""

    api_key: str
    key_prefix: str
    warning: str = "Save this key now. It cannot be retrieved later."
    created_at: datetime


class APIKeyInfo(BaseModel):
    """API key information (masked)."""

    has_key: bool
    key_prefix: str | None = None
    key_masked: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    status: str | None = None


class APIKeyRegenerateResponse(BaseModel):
    """API key regeneration response."""

    api_key: str
    key_prefix: str
    warning: str = "Save this key now. It cannot be retrieved later."
    old_key_prefix: str


# Usage statistics schemas
class QuotaInfo(BaseModel):
    """User quota information."""

    has_key: bool
    daily_limit_usd: float | None = None
    monthly_limit_usd: float | None = None
    spent_today_usd: float | None = None
    spent_month_usd: float | None = None
    remaining_today_usd: float | None = None


class UsageStats(BaseModel):
    """User usage statistics."""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class UsageResponse(BaseModel):
    """User usage response."""

    period: str
    quota: QuotaInfo
    usage: UsageStats


# Password reset schemas (optional, for future)
class ForgotPasswordRequest(BaseModel):
    """Forgot password request."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Reset password request."""

    token: str
    new_password: str  # Validate strength in handler to return 400


class PasswordResetResponse(BaseModel):
    """Password reset response."""

    message: str


class ResendVerificationRequest(BaseModel):
    """Resend email verification request."""

    email: EmailStr


class ResendVerificationResponse(BaseModel):
    """Resend verification response."""

    message: str


class ChangePasswordRequest(BaseModel):
    """Change password request (for logged-in users)."""

    old_password: str
    new_password: str  # Validate strength in handler to return 400


class ChangePasswordResponse(BaseModel):
    """Change password response."""

    message: str


class ChangeEmailRequest(BaseModel):
    """Change email request."""

    new_email: EmailStr
    password: str  # Require password confirmation


class ChangeEmailResponse(BaseModel):
    """Change email response."""

    message: str
    new_email: str
