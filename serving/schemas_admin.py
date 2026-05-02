"""Pydantic schemas for admin API endpoints."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator


class CreateAPIKeyRequest(BaseModel):  # type: ignore[no-any-unimported]
    """Request payload for creating a new API key."""

    user_id: str = Field(..., min_length=1, max_length=255, description="Unique user identifier")
    user_name: str | None = Field(None, max_length=255, description="Display name for the user")
    quota_daily_cost_usd: Decimal = Field(
        Decimal("1000.00"),
        ge=0,
        description="Daily cost quota in USD",
    )
    quota_monthly_cost_usd: Decimal | None = Field(
        None,
        ge=0,
        description="Monthly cost quota in USD (optional)",
    )
    expires_at: datetime | None = Field(
        None,
        description="Expiration timestamp (optional)",
    )
    notes: str | None = Field(None, description="Admin notes")
    metadata: dict[str, Any] | None = Field(None, description="Custom metadata")

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        """Ensure user_id contains only safe characters."""
        if not v.replace("_", "").replace("-", "").replace(".", "").isalnum():
            raise ValueError("user_id must contain only alphanumeric, dash, underscore, or dot")
        return v


class CreateAPIKeyResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response payload for successful API key creation."""

    api_key: str = Field(..., description="Plaintext API key (shown only once)")
    user_id: str
    key_prefix: str = Field(..., description="First 12 characters for identification")
    quota_daily_cost_usd: Decimal
    quota_monthly_cost_usd: Decimal | None
    expires_at: datetime | None
    created_at: datetime
    warning: str = Field(
        default="⚠️ Save this API key now. It cannot be retrieved later.",
        description="Security warning",
    )


class APIKeyListItem(BaseModel):  # type: ignore[no-any-unimported]
    """Single API key item in list view."""

    user_id: str
    user_name: str | None
    key_prefix: str
    status: str
    quota_daily_cost_usd: Decimal
    quota_monthly_cost_usd: Decimal | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    usage_today_usd: Decimal = Field(default=Decimal("0"), description="Cost spent today")
    usage_month_usd: Decimal = Field(default=Decimal("0"), description="Cost spent this month")
    notes: str | None


class ListAPIKeysResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response payload for listing API keys."""

    total: int
    keys: list[APIKeyListItem]


class APIKeyDetailUsage(BaseModel):  # type: ignore[no-any-unimported]
    """Detailed usage statistics for a user."""

    today: dict[str, Any] = Field(
        default_factory=dict,
        description="Today's usage: cost_usd, requests, quota_remaining_usd",
    )
    this_month: dict[str, Any] = Field(
        default_factory=dict,
        description="This month's usage: cost_usd, requests, quota_remaining_usd",
    )
    models_used: list[str] = Field(default_factory=list, description="List of models accessed")
    last_request_at: datetime | None = Field(None, description="Timestamp of last request")


class APIKeyDetailResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response payload for detailed API key view."""

    user_id: str
    user_name: str | None
    key_prefix: str
    status: str
    quota_daily_cost_usd: Decimal
    quota_monthly_cost_usd: Decimal | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    notes: str | None
    metadata: dict[str, Any] | None
    usage: APIKeyDetailUsage


class UpdateAPIKeyRequest(BaseModel):  # type: ignore[no-any-unimported]
    """Request payload for updating an API key."""

    user_name: str | None = Field(None, max_length=255)
    status: str | None = Field(None, pattern="^(active|suspended|revoked)$")
    quota_daily_cost_usd: Decimal | None = Field(None, ge=0)
    quota_monthly_cost_usd: Decimal | None = Field(None, ge=0)
    expires_at: datetime | None = None
    notes: str | None = None
    metadata: dict[str, Any] | None = None


class UpdateAPIKeyResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response payload for successful update."""

    user_id: str
    updated_fields: list[str]
    new_values: dict[str, Any]


class RevokeAPIKeyResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response payload for successful revocation."""

    user_id: str
    action: str  # "revoked" or "deleted"
    message: str


class RegenerateAPIKeyResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response payload for successful key regeneration."""

    api_key: str = Field(..., description="New plaintext API key (shown only once)")
    user_id: str
    key_prefix: str
    old_key_prefix: str
    warning: str = Field(
        default="⚠️ Old key is now revoked. Save this new key immediately.",
        description="Security warning",
    )


# ========================================
# User Registration Management Schemas
# ========================================


class UserListItem(BaseModel):
    """Single user item in admin user list.

    Includes API key status and usage data via LEFT JOIN.
    """

    id: str
    email: str
    user_name: str | None
    role: str = "free"
    status: str
    email_verified: bool
    approval_note: str | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None
    # API key info (populated via LEFT JOIN)
    has_key: bool = False
    key_prefix: str | None = None
    key_status: str | None = None
    usage_today_usd: Decimal = Field(default=Decimal("0"))
    usage_month_usd: Decimal = Field(default=Decimal("0"))
    usage_alltime_usd: Decimal = Field(default=Decimal("0"))


class StatusCounts(BaseModel):
    """Per-status user counts (always unfiltered)."""

    all: int = 0
    pending_approval: int = 0
    active: int = 0
    suspended: int = 0
    rejected: int = 0
    deleted: int = 0


class ListUsersResponse(BaseModel):
    """Response payload for listing users."""

    total: int
    users: list[UserListItem]
    status_counts: StatusCounts = Field(default_factory=StatusCounts)


class ApproveUserRequest(BaseModel):
    """Request payload for approving a user registration."""

    note: str | None = Field(None, max_length=500, description="Optional approval note")


class ApproveUserResponse(BaseModel):
    """Response payload for successful user approval."""

    user_id: str
    email: str
    status: str
    message: str


class RejectUserRequest(BaseModel):
    """Request payload for rejecting a user registration."""

    reason: str = Field(
        ..., min_length=1, max_length=500, description="Reason for rejection (sent to user)"
    )


class RejectUserResponse(BaseModel):
    """Response payload for successful user rejection."""

    user_id: str
    email: str
    status: str
    message: str


class UserDetailResponse(BaseModel):
    """Detailed user info including usage analytics."""

    id: str
    email: str
    user_name: str | None
    role: str = "free"
    status: str
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None = None
    # Key info
    has_key: bool = False
    key_prefix: str | None = None
    quota_daily_usd: float | None = None
    quota_monthly_usd: float | None = None
    # Usage
    usage_today_usd: float = 0.0
    usage_today_requests: int = 0
    usage_month_usd: float = 0.0
    usage_month_requests: int = 0
    models_used: list[str] = Field(default_factory=list)
    last_request_at: datetime | None = None


class UpdateUserRequest(BaseModel):
    """Request payload for updating user/key settings."""

    role: str | None = Field(
        None,
        pattern="^(free|pro|internal|admin)$",
        description="One of: free, pro, internal, admin",
    )
    status: str | None = Field(None, pattern="^(active|suspended)$")
    quota_daily_cost_usd: Decimal | None = Field(None, ge=0)
    quota_monthly_cost_usd: Decimal | None = Field(None, ge=0)


class UpdateUserResponse(BaseModel):
    """Response payload for successful user update."""

    user_id: str
    updated_fields: list[str]
    message: str


# ========================================
# Audit Log Schemas
# ========================================


class AuditLogEntry(BaseModel):
    """Single entry from the admin audit log."""

    id: int
    timestamp: datetime
    admin_ip: str
    action: str
    target_user_id: str | None = None
    details: dict[str, Any] | None = None
    success: bool = True


class ListAuditLogResponse(BaseModel):
    """Response payload for listing audit log entries."""

    total: int
    entries: list[AuditLogEntry]


# ========================================
# Delete User Schemas
# ========================================


class DeleteUserRequest(BaseModel):
    """Request payload for soft-deleting a user."""

    reason: str = Field(
        ..., min_length=1, max_length=500, description="Reason for deletion (audit trail)"
    )

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: str) -> str:
        """Strip whitespace and reject blank reasons."""
        v = v.strip()
        if not v:
            raise ValueError("Reason must not be blank")
        return v


class DeleteUserResponse(BaseModel):
    """Response payload for successful user deletion."""

    user_id: str
    email: str
    status: str
    message: str


# ========================================
# Admin Recent Requests Schemas
# ========================================


class AdminRequestMetricsBucket(BaseModel):
    """A single request-count bucket for admin traffic charts."""

    start_time: datetime
    request_count: int
    success_count: int
    error_count: int
    avg_latency_ms: float | None = None


class AdminRequestMetricsWindow(BaseModel):
    """Request metrics for a fixed lookback window."""

    key: str
    label: str
    window_minutes: int
    bucket_minutes: int
    total_requests: int
    success_requests: int
    error_requests: int
    avg_latency_ms: float | None = None
    buckets: list[AdminRequestMetricsBucket]


class AdminRequestMetricsResponse(BaseModel):
    """Request metrics for multiple admin dashboard lookback windows."""

    generated_at: datetime
    windows: list[AdminRequestMetricsWindow]


class AdminHistogramBucket(BaseModel):
    """A single histogram bucket for a metric distribution."""

    lower_bound: float
    upper_bound: float | None = None
    count: int


class AdminMetricDistribution(BaseModel):
    """Distribution summary (count, percentiles, histogram) for a single metric."""

    count: int
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    p50: float | None = None
    p90: float | None = None
    p95: float | None = None
    p99: float | None = None
    histogram: list[AdminHistogramBucket] = Field(default_factory=list)


class AdminPerformanceMetricsWindow(BaseModel):
    """Performance metric distributions for a single lookback window."""

    key: str
    label: str
    window_minutes: int
    prompt_tokens: AdminMetricDistribution
    completion_tokens: AdminMetricDistribution
    ttft_ms: AdminMetricDistribution
    tbt_ms: AdminMetricDistribution


class AdminPerformanceMetricsResponse(BaseModel):
    """Performance metric distributions across admin dashboard lookback windows."""

    generated_at: datetime
    windows: list[AdminPerformanceMetricsWindow]


class AdminRecentRequestItem(BaseModel):
    """A single API request log entry (admin view, includes user identity)."""

    request_id: str
    user_id: str | None = None
    user_name: str | None = None
    user_email: str | None = None
    user_ip: str | None = None
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
    total_tokens: int | None = None
    cost_usd: float | None = None
    prompt: str | None = None
    response: str | None = None
    error: str | None = None


class AdminRecentRequestsResponse(BaseModel):
    """Paginated list of recent requests across all users (admin view)."""

    requests: list[AdminRecentRequestItem]
    total: int
    limit: int
    offset: int


# ── Analytics Dashboard ──────────────────────────────────────────────────────


class SparklineBucket(BaseModel):
    """One time bucket for the active-users sparkline."""

    start_time: datetime
    request_count: int


class AnalyticsUserEntry(BaseModel):
    """One row in the top-users horizontal bar chart."""

    email: str
    user_id: str
    requests: int
    fraction: float  # share of user-attributed requests in the period (0.0-1.0)


class AnalyticsBreakdownEntry(BaseModel):
    """One slice in a model or provider donut chart."""

    name: str  # model_id / provider name; "others" for the collapsed remainder
    requests: int
    fraction: float  # share of total requests in the period


class AdminAnalyticsResponse(BaseModel):
    """Response for GET /admin/analytics."""

    period: str = Field(..., pattern="^(hour|day|week|month)$")
    active_users: int
    sparkline: list[SparklineBucket]
    top_users: list[AnalyticsUserEntry]
    by_model: list[AnalyticsBreakdownEntry]
    by_provider: list[AnalyticsBreakdownEntry]
    generated_at: datetime


# ========================================
# Provider Quotas (Admin Dashboard)
# ========================================


class ProviderQuotaUsage(BaseModel):
    """A single usage measurement for a provider (e.g. monthly cost, request count)."""

    label: str = Field(..., description="Human-readable label, e.g. 'Monthly', '4-hour window'")
    used: float | None = Field(None, description="Amount consumed (None if unknown)")
    limit: float | None = Field(
        None, description="Total quota limit (None if unlimited or unknown)"
    )
    unit: str = Field(..., description="Unit string, e.g. 'USD', 'tokens', 'requests'")
    reset_at: datetime | None = Field(None, description="When this usage window resets (UTC)")


class ProviderQuotaResult(BaseModel):
    """Result of querying a single upstream provider's quota."""

    name: str = Field(..., description="Lowercase identifier: chutes | zai | minimax | ollama")
    display_name: str = Field(..., description="Human-readable name")
    key_configured: bool = Field(..., description="True if credentials are present in env")
    key_masked: str | None = Field(None, description="Masked key/cookie (None if not configured)")
    fetched_at: datetime | None = Field(None, description="When the quota was fetched (UTC)")
    ok: bool = Field(..., description="True if quota fetch succeeded")
    error: str | None = Field(
        None,
        description="Short reason code if !ok: 'auth_failed' | 'timeout' | 'not_configured' | 'parse_error' | 'unexpected'",
    )
    usages: list[ProviderQuotaUsage] = Field(default_factory=list)


class AdminProviderQuotasResponse(BaseModel):
    """Aggregated response for the admin provider-quotas endpoint."""

    generated_at: datetime
    providers: list[ProviderQuotaResult]


# Rebuild models to ensure forward references are resolved when imported via FastAPI
__all__ = [
    "APIKeyDetailResponse",
    "APIKeyDetailUsage",
    "APIKeyListItem",
    "AdminAnalyticsResponse",
    "AdminHistogramBucket",
    "AdminMetricDistribution",
    "AdminPerformanceMetricsResponse",
    "AdminPerformanceMetricsWindow",
    "AdminProviderQuotasResponse",
    "AdminRecentRequestItem",
    "AdminRecentRequestsResponse",
    "AdminRequestMetricsBucket",
    "AdminRequestMetricsResponse",
    "AdminRequestMetricsWindow",
    "AnalyticsBreakdownEntry",
    "AnalyticsUserEntry",
    "ApproveUserRequest",
    "ApproveUserResponse",
    "AuditLogEntry",
    "CreateAPIKeyRequest",
    "CreateAPIKeyResponse",
    "DeleteUserRequest",
    "DeleteUserResponse",
    "ListAPIKeysResponse",
    "ListAuditLogResponse",
    "ListUsersResponse",
    "ProviderQuotaResult",
    "ProviderQuotaUsage",
    "RegenerateAPIKeyResponse",
    "RejectUserRequest",
    "RejectUserResponse",
    "RevokeAPIKeyResponse",
    "SparklineBucket",
    "StatusCounts",
    "UpdateAPIKeyRequest",
    "UpdateAPIKeyResponse",
    "UpdateUserRequest",
    "UpdateUserResponse",
    "UserDetailResponse",
    "UserListItem",
]


# ── Broadcast Email Schemas ────────────────────────────────────────────────


class BroadcastPreviewRequest(BaseModel):
    template_key: str | None = None
    template_vars: dict = Field(default_factory=dict)
    subject: str = Field("", description="Required when template_key is None")
    body_html: str = Field("", description="Required when template_key is None")
    body_text: str = Field("", description="Required when template_key is None")
    # Empty arrays would silently match zero users (postgres ANY('{}') is always
    # false), which is confusing for admins. Require at least one role and one
    # status — admin must opt in to who receives the broadcast.
    target_roles: list[str] = Field(..., min_length=1)
    target_statuses: list[str] = Field(..., min_length=1)


class BroadcastPreviewResponse(BaseModel):
    recipient_count: int
    rendered_subject: str
    rendered_body_html: str
    rendered_body_text: str


class CreateBroadcastRequest(BroadcastPreviewRequest):
    scheduled_at: datetime | None = None


class CreateBroadcastResponse(BaseModel):
    id: str
    status: str
    recipient_count: int
    scheduled_at: datetime | None


class BroadcastListItem(BaseModel):
    id: str
    subject: str
    status: str
    recipient_count: int
    scheduled_at: datetime | None
    sent_at: datetime | None
    created_by: str
    created_at: datetime


class ListBroadcastsResponse(BaseModel):
    total: int
    broadcasts: list[BroadcastListItem]


class BroadcastRecipientItem(BaseModel):
    user_id: str
    email: str
    status: str
    error: str | None
    sent_at: datetime | None


class BroadcastDetailResponse(BaseModel):
    broadcast: BroadcastListItem
    recipients: list[BroadcastRecipientItem]
    total_recipients: int
