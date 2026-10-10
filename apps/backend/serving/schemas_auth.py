"""Pydantic schemas for authentication and user management."""

import re
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, EmailStr, Field, StringConstraints, field_validator
from pydantic.networks import validate_email

from serving.config.site_identity import get_site_identity
from serving.utils.password import validate_password_strength

UserName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=50)]

# The sign-in name of an account created without an email address (the
# first-run setup administrator). Stored lowercased; it can never contain
# ``@``, which is how sign-in tells it from an email address.
LOGIN_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")


def normalize_login_name(value: str) -> str:
    """Return *value* as a login name: trimmed, lowercased and checked.

    Raises:
        ValueError: If it is not 3-32 letters, digits, ``.``, ``_`` or ``-``,
            starting with a letter or digit (``@`` is never allowed).
    """
    name = value.strip().lower()
    if not LOGIN_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Username must be 3-32 characters of letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return name


# Authentication request/response schemas
class SignupRequest(BaseModel):
    """User signup request."""

    email: EmailStr
    password: str = Field(..., min_length=8)
    user_name: UserName
    use_case: str | None = Field(default=None, max_length=2000)
    accepted_tos: bool
    turnstile_token: str | None = None


class SignupResponse(BaseModel):
    """User signup response."""

    message: str
    email: str
    user_id: str
    requires_approval: bool = False


class LoginRequest(BaseModel):
    """User login request.

    ``email`` keeps its name for compatibility but takes either an email
    address or, for an account created without one, a login name. A value
    containing ``@`` is validated and normalized exactly as an ``EmailStr``;
    anything else must be a valid login name and is lowercased.
    """

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email_or_login_name(cls, value: str) -> str:
        value = value.strip()
        if "@" in value:
            return validate_email(value)[1]
        return normalize_login_name(value)


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
    """User information (public).

    ``email`` is None for an account created without one (the first-run setup
    administrator), which has a ``login_name`` instead.
    """

    id: str
    email: str | None = None
    login_name: str | None = None
    user_name: str | None = None
    role: str = "free"
    status: str = "active"
    email_verified: bool = False
    is_admin: bool = False
    created_at: datetime
    last_login_at: datetime | None = None


# First-run setup schemas
class SetupStatusResponse(BaseModel):
    """Whether the console should show the first-run setup page."""

    setup_required: bool
    database_enabled: bool


class SetupAdminRequest(BaseModel):
    """First-run setup: the one-time code and the administrator to create.

    Field problems are 422s attributed to their field, so the setup page can
    show each next to its input.
    """

    setup_code: str = Field(..., max_length=64)
    login_name: str = Field(..., max_length=64)
    password: str
    display_name: str | None = Field(default=None, max_length=200)

    @field_validator("login_name")
    @classmethod
    def _check_login_name(cls, value: str) -> str:
        return normalize_login_name(value)

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        is_valid, error_msg = validate_password_strength(value)
        if not is_valid:
            raise ValueError(error_msg or "Password does not meet security requirements")
        return value

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not 2 <= len(value) <= 50:
            raise ValueError("Display name must be 2-50 characters")
        return value


class UserProfileUpdate(BaseModel):
    """User profile update request."""

    user_name: UserName | None = None


class LLMProberLayoutState(BaseModel):
    """Persisted llm-prober layout for one user account."""

    direct_models: list[str] = Field(default_factory=list)
    direct_providers: dict[str, list[str]] = Field(default_factory=dict)
    e2e_models: list[str] = Field(default_factory=list)


class LLMProberLayoutResponse(BaseModel):
    """Current llm-prober layout response."""

    layout: LLMProberLayoutState


# API key schemas
class APIKeyCreate(BaseModel):
    """API key creation request (no body needed)."""

    pass


class APIKeyResponse(BaseModel):
    """API key creation response."""

    api_key: str
    key_prefix: str
    warning: str = "Save this API key now. It will not be shown again."
    created_at: datetime


class APIKeyInfo(BaseModel):
    """API key information (masked)."""

    has_key: bool
    api_key: str | None = None
    key_prefix: str | None = None
    key_masked: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    status: str | None = None


class APIKeyListItem(BaseModel):
    """Single API key record for the current user."""

    api_key: str | None = None
    key_prefix: str
    key_masked: str
    created_at: datetime
    last_used_at: datetime | None = None
    status: str


class APIKeyListResponse(BaseModel):
    """All API keys owned by the current user."""

    keys: list[APIKeyListItem]


class APIKeyDeleteResponse(BaseModel):
    """Response returned after revoking an API key."""

    key_prefix: str
    status: str
    message: str


class APIKeyRegenerateResponse(BaseModel):
    """API key regeneration response."""

    api_key: str
    key_prefix: str
    warning: str = "Save this API key now. It will not be shown again."
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
    max_concurrency: int | None = None
    reset_at: datetime | None = None
    reset_timezone: str = "UTC"
    contact_email: str = Field(default_factory=lambda: get_site_identity().support_email)
    increase_request_message: str = Field(default_factory=lambda: _quota_message())


def _quota_message() -> str:
    """Ask for more quota, naming an address only when there is one.

    A deployment that has configured no support address renders the empty
    string, and "Email  and explain your use case." reads as a bug in the
    product rather than as a gap in its configuration. Same rule as the 429
    path and the OpenRouter attribution headers: say the true thing or say
    nothing, never say a blank.
    """
    contact = get_site_identity().support_email
    if contact:
        return f"Need more quota? Email {contact} and explain your use case."
    return "Need more quota? Contact the operator of this deployment and explain your use case."


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


# Recent requests schemas
class RecentRequestItem(BaseModel):
    """A single API request log entry (user-facing)."""

    request_id: str
    model_id: str
    provider: str
    timestamp: datetime
    status_code: int | None = None
    latency_ms: int | None = None
    ttft_ms: int | None = None
    stream: bool | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    routewise: dict[str, Any] | None = None
    # "embedding" for /v1/embeddings traffic; None (legacy) implies a
    # chat/completion request.
    request_type: str | None = None


class RecentRequestsResponse(BaseModel):
    """Paginated list of recent requests for the current user."""

    requests: list[RecentRequestItem]
    total: int
    limit: int
    offset: int
