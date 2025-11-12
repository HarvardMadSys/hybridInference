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

    key_prefix: str
    key_masked: str
    created_at: datetime
    last_used_at: datetime | None = None
    status: str = "active"


class APIKeyRegenerateResponse(BaseModel):
    """API key regeneration response."""

    api_key: str
    key_prefix: str
    warning: str = "Save this key now. It cannot be retrieved later."
    old_key_prefix: str


# Usage statistics schemas
class QuotaInfo(BaseModel):
    """User quota information."""

    daily_limit_usd: float
    monthly_limit_usd: float | None = None
    spent_today_usd: float
    spent_month_usd: float
    remaining_today_usd: float


class UsageStats(BaseModel):
    """User usage statistics."""

    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


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
    new_password: str = Field(..., min_length=8)


class PasswordResetResponse(BaseModel):
    """Password reset response."""

    message: str
