"""Pydantic schemas for admin API endpoints."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

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


# ========================================
# User Cost History & Summary Schemas
# ========================================


class UserCostHistoryPoint(BaseModel):
    """One day of per-user cost data."""

    day: str  # "YYYY-MM-DD" UTC
    cost_usd: Decimal = Field(default=Decimal("0"))
    requests: int = 0


class UserCostHistoryResponse(BaseModel):
    """Daily cost history for a single user."""

    user_id: str
    days: int
    points: list[UserCostHistoryPoint]


class BulkUserCostHistoryResponse(BaseModel):
    """Daily cost history for many users (one round-trip per page)."""

    days: int
    histories: dict[str, list[UserCostHistoryPoint]]  # keyed by user_id


class SummaryUserItem(BaseModel):
    """User entry inside a summary card (sub-set of UserListItem)."""

    id: str
    email: str
    user_name: str | None = None
    role: str = "free"
    today_cost_usd: Decimal = Field(default=Decimal("0"))
    avg_prior_7d_usd: Decimal = Field(default=Decimal("0"))
    quota_daily_usd: float | None = None
    multiplier: float | None = None  # today / avg, anomaly card only


class SummaryCard(BaseModel):
    """A single summary-card payload: count + top examples."""

    count: int
    top: list[SummaryUserItem]


class UsersSummaryResponse(BaseModel):
    """Aggregated counts and exemplar users for the 4 dashboard cards."""

    pending: SummaryCard
    top_spenders_today: SummaryCard
    anomalies: SummaryCard
    near_quota: SummaryCard


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
    disabled_models: list[str] = Field(default_factory=list)
    last_request_at: datetime | None = None
    max_concurrent_requests: int | None = None


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
    disabled_models: list[str] | None = None
    max_concurrent_requests: int | None = Field(None, ge=1)


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


class ResumeUserRequest(BaseModel):
    """Request payload for resuming a soft-deleted user."""

    reason: str | None = Field(None, max_length=500, description="Optional reason (audit trail)")


class ResumeUserResponse(BaseModel):
    """Response payload for successful user resume."""

    user_id: str
    email: str
    status: str
    message: str


class HardDeleteUserRequest(BaseModel):
    """Request payload for permanently deleting a user.

    Requires explicit ``confirm=True``.  Defense-in-depth — the API still
    rejects requests where the value is missing or false even though the
    UI always sends ``true``.
    """

    confirm: bool = Field(..., description="Must be true to proceed")
    reason: str | None = Field(None, max_length=500, description="Optional reason (audit trail)")


class HardDeleteUserResponse(BaseModel):
    """Response payload for successful permanent deletion."""

    user_id: str
    email: str
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
    throughput_tps: AdminMetricDistribution


class AdminPerformanceMetricsResponse(BaseModel):
    """Performance metric distributions across admin dashboard lookback windows."""

    generated_at: datetime
    windows: list[AdminPerformanceMetricsWindow]


class AdminTtftScatterPoint(BaseModel):
    """Single point on the TTFT-vs-input-length scatter plot."""

    prompt_tokens: int
    ttft_ms: int
    cache_hit: bool
    timestamp: datetime


class AdminTtftScatterModel(BaseModel):
    """Scatter points for one (model_id, provider) pair.

    Models with fallbacks are routed across multiple upstream providers,
    each with its own TTFT profile, so we keep them as separate series.
    """

    model_id: str
    provider: str
    points: list[AdminTtftScatterPoint]


class AdminTtftScatterResponse(BaseModel):
    """TTFT vs input length scatter data, grouped by (model_id, provider)."""

    models: list[AdminTtftScatterModel]


class AdminRecentRequestItem(BaseModel):
    """A single API request log entry (admin view, includes user identity)."""

    request_id: str
    user_id: str | None = None
    user_name: str | None = None
    user_email: str | None = None
    user_ip: str | None = None
    peer_ip: str | None = None
    ip_source: str | None = None
    x_forwarded_for: str | None = None
    user_agent: str | None = None
    session_id: str | None = None
    request_surface: str | None = None
    model_id: str
    provider: str
    timestamp: datetime
    status_code: int | None = None
    latency_ms: int | None = None
    ttft_ms: int | None = None
    decode_throughput_tps: float | None = None
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
    # Conversation shape derived from the stored request payload's messages
    # array. None when the payload is absent (e.g. legacy rows) or not a chat
    # request. num_turns counts all messages; num_user_turns counts user-role
    # messages; num_tool_calls sums tool_calls across assistant messages.
    num_turns: int | None = None
    num_user_turns: int | None = None
    num_tool_calls: int | None = None


class AdminRecentRequestsResponse(BaseModel):
    """Paginated list of recent requests across all users (admin view)."""

    requests: list[AdminRecentRequestItem]
    total: int
    limit: int
    offset: int


class AdminRecentRequestContentResponse(BaseModel):
    """Prompt/response payload for a single api_logs row (admin on-demand fetch)."""

    prompt: str | None = None
    response: str | None = None
    reasoning_content: str | None = None


class AdminClearErrorRequestsResponse(BaseModel):
    """Result of clearing recent error requests from api_logs."""

    deleted_count: int
    hours: int
    message: str


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

    name: str = Field(
        ..., description="Lowercase identifier: chutes | zai | minimax | kimi | ollama"
    )
    display_name: str = Field(..., description="Human-readable name")
    key_index: int | None = Field(
        None,
        description="1-based key index when provider has multiple keys; None for single-key providers",
    )
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


class RuntimeSettingItem(BaseModel):
    """A single runtime setting with current value and metadata."""

    key: str
    value: Any
    value_type: str
    default_value: Any
    description: str
    min: float | None = None
    max: float | None = None


class ListSettingsResponse(BaseModel):
    """Response payload for listing runtime settings."""

    settings: list[RuntimeSettingItem]


class UpdateSettingRequest(BaseModel):
    """Request payload for updating a runtime setting."""

    value: Any


class RoutewiseSettingItem(BaseModel):
    """A curated Routewise runtime setting with current value and metadata."""

    key: Literal[
        "routewise_latency_slo_sec",
        "routewise_latency_min_samples",
    ]
    value: Any
    value_type: Literal["str", "int", "float"]
    default_value: Any
    description: str
    min: int | float | None = None
    max: int | float | None = None


class ListRoutewiseSettingsResponse(BaseModel):
    """Response payload for listing Routewise runtime settings."""

    settings: list[RoutewiseSettingItem]


class ModelVisibilityItem(BaseModel):
    """Current visibility requirements for a canonical model."""

    model_id: str
    baseline_required_role: str
    override_required_role: str | None = None
    effective_required_role: str


class ListModelVisibilityResponse(BaseModel):
    """Response payload for listing model visibility."""

    models: list[ModelVisibilityItem]


class UpdateModelVisibilityRequest(BaseModel):
    """Request payload for updating a model visibility override."""

    required_role: Literal["free", "pro", "internal", "admin"] | None


class ModelConcurrencyItem(BaseModel):
    """Current per-user concurrency-limit exemption state for a canonical model."""

    model_id: str
    exempt: bool


class ListModelConcurrencyResponse(BaseModel):
    """Response payload for listing model concurrency exemptions."""

    models: list[ModelConcurrencyItem]


class UpdateModelConcurrencyRequest(BaseModel):
    """Request payload for updating a model concurrency exemption."""

    exempt: bool


class RouteWeightItem(BaseModel):
    """Current route weight state for one model endpoint."""

    model_id: str
    strategy: str
    endpoint_id: str
    provider: str
    base_url: str | None = None
    yaml_weight: float
    override_weight: float | None = None
    effective_weight: float


class ListRouteWeightsResponse(BaseModel):
    """Response payload for listing route weights for a model."""

    model_id: str
    routes: list[RouteWeightItem]


class ListAllRouteWeightsResponse(BaseModel):
    """Response payload for listing route weights across canonical models."""

    routes: list[RouteWeightItem]


class UpdateRouteWeightRequest(BaseModel):
    """Request payload for upserting a route weight override."""

    weight: float


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
    "AdminRecentRequestContentResponse",
    "AdminRecentRequestItem",
    "AdminRecentRequestsResponse",
    "AdminRequestMetricsBucket",
    "AdminRequestMetricsResponse",
    "AdminRequestMetricsWindow",
    "AdminTtftScatterModel",
    "AdminTtftScatterPoint",
    "AdminTtftScatterResponse",
    "AnalyticsBreakdownEntry",
    "AnalyticsUserEntry",
    "ApproveUserRequest",
    "ApproveUserResponse",
    "AuditLogEntry",
    "BulkUserCostHistoryResponse",
    "CreateAPIKeyRequest",
    "CreateAPIKeyResponse",
    "DeleteUserRequest",
    "DeleteUserResponse",
    "HardDeleteUserRequest",
    "HardDeleteUserResponse",
    "ListAPIKeysResponse",
    "ListAllRouteWeightsResponse",
    "ListAuditLogResponse",
    "ListModelVisibilityResponse",
    "ListRouteWeightsResponse",
    "ListRoutewiseSettingsResponse",
    "ListSettingsResponse",
    "ListSignupAllowedDomainsResponse",
    "ListUsersResponse",
    "ModelVisibilityItem",
    "ProviderQuotaResult",
    "ProviderQuotaUsage",
    "RegenerateAPIKeyResponse",
    "RejectUserRequest",
    "RejectUserResponse",
    "ResumeUserRequest",
    "ResumeUserResponse",
    "RevokeAPIKeyResponse",
    "RouteWeightItem",
    "RoutewiseSettingItem",
    "RuntimeSettingItem",
    "SparklineBucket",
    "StatusCounts",
    "SummaryCard",
    "SummaryUserItem",
    "UpdateAPIKeyRequest",
    "UpdateAPIKeyResponse",
    "UpdateModelVisibilityRequest",
    "UpdateRouteWeightRequest",
    "UpdateSettingRequest",
    "UpdateUserRequest",
    "UpdateUserResponse",
    "UserCostHistoryPoint",
    "UserCostHistoryResponse",
    "UserDetailResponse",
    "UserListItem",
    "UsersSummaryResponse",
]


# ── Broadcast Email Schemas ────────────────────────────────────────────────


class BroadcastPreviewRequest(BaseModel):
    template_key: str | None = None
    template_vars: dict = Field(default_factory=dict)
    subject: str = Field("", description="Required when template_key is None")
    body_html: str = Field("", description="Required when template_key is None")
    body_text: str = Field("", description="Required when template_key is None")
    body_markdown: str = Field(
        "",
        description=(
            "Markdown source for custom body; rendered to HTML server-side. "
            "Takes precedence over body_html when set."
        ),
    )
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


# ============================================================
# Provider Performance (admin /admin/api/provider-stats)
# ============================================================


class ProviderStatsRow(BaseModel):
    hour_bucket: datetime
    provider: str
    model_id: str

    request_count: int
    error_count: int
    stream_count: int

    ttft_p50_ms: int | None = None
    ttft_p95_ms: int | None = None
    ttft_p99_ms: int | None = None

    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    latency_p99_ms: int | None = None

    throughput_avg_tps: float | None = None
    throughput_p50_tps: float | None = None
    throughput_p95_tps: float | None = None

    prompt_tokens_avg: float | None = None
    completion_tokens_avg: float | None = None
    total_completion_tokens: int
    total_prompt_tokens: int | None = None
    total_reasoning_tokens: int | None = None


class ProviderModelPair(BaseModel):
    provider: str
    model_id: str


class ProviderStatsResponse(BaseModel):
    rows: list[ProviderStatsRow]
    # `providers`/`models`/`pairs` span the full retained table (last 30
    # days), so the dropdowns stay populated even when the selected range
    # has no rows.
    providers: list[str]
    models: list[str]
    pairs: list[ProviderModelPair]
    # Providers that actually have rows inside the selected [from, to)
    # window. The UI prefers one of these as the default selection so the
    # tab doesn't render empty on load when a retention-only provider sorts
    # first.
    window_providers: list[str]


# ============================================================
# Provider Token Usage (per-provider, per-model token totals over a
# selectable hourly window). Powers the admin "Token Usage" tab.
# ============================================================


class ProviderTokenUsageRow(BaseModel):  # type: ignore[no-any-unimported]
    provider: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    request_count: int


class ProviderTokenUsageTotals(BaseModel):  # type: ignore[no-any-unimported]
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    request_count: int


class ProviderTokenUsageWindow(BaseModel):  # type: ignore[no-any-unimported]
    from_: datetime = Field(alias="from")
    to: datetime

    model_config = {"populate_by_name": True}


class ProviderTokenUsageResponse(BaseModel):  # type: ignore[no-any-unimported]
    range: Literal["1h", "24h", "7d", "30d"]
    window: ProviderTokenUsageWindow
    refreshed_at: datetime
    rows: list[ProviderTokenUsageRow]
    totals: ProviderTokenUsageTotals


# ---------------------------------------------------------------------------
# Signup domain allowlist
# ---------------------------------------------------------------------------


class SignupAllowedDomain(BaseModel):  # type: ignore[no-any-unimported]
    """Single allowlist entry returned by the admin API."""

    domain: str
    is_wildcard: bool
    created_at: datetime | None = None
    created_by: str | None = None
    created_by_email: str | None = None


class ListSignupAllowedDomainsResponse(BaseModel):  # type: ignore[no-any-unimported]
    """List response for ``GET /admin/signup-domains``."""

    domains: list[SignupAllowedDomain]


class AddSignupAllowedDomainRequest(BaseModel):  # type: ignore[no-any-unimported]
    """Request body for ``POST /admin/signup-domains``."""

    domain: str = Field(..., min_length=1, max_length=255)


# ---------------------------------------------------------------------------
# Provider API keys (admin-managed runtime credentials)
# ---------------------------------------------------------------------------


class ProviderApiKeyItem(BaseModel):  # type: ignore[no-any-unimported]
    """Single masked provider API key row in the admin list view."""

    id: str | None = Field(
        None,
        description="Row id (None for env-var-sourced entries)",
    )
    provider: str
    key_prefix: str
    label: str | None = None
    source: Literal["env", "db"]
    status: str = "active"
    created_at: datetime | None = None


class ListProviderApiKeysResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response for ``GET /admin/provider-keys``."""

    provider: str | None = None
    keys: list[ProviderApiKeyItem]


class AddProviderApiKeyRequest(BaseModel):  # type: ignore[no-any-unimported]
    """Request body for ``POST /admin/provider-keys``."""

    provider: str = Field(..., min_length=1, max_length=64)
    api_key: str = Field(..., min_length=1, max_length=4096)
    label: str | None = Field(None, max_length=255)


class DisableProviderEnvKeyRequest(BaseModel):  # type: ignore[no-any-unimported]
    """Request body for disabling an env-sourced provider API key."""

    provider: str = Field(..., min_length=1, max_length=64)
    env_key_id: str = Field(..., min_length=1, max_length=128)


class AddProviderApiKeyResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response for ``POST /admin/provider-keys``."""

    key: ProviderApiKeyItem
    pools_updated: int = Field(
        ...,
        description="Number of in-process key pools the new key was injected into",
    )


class DeleteProviderApiKeyResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response for ``DELETE /admin/provider-keys/{id}``."""

    id: str
    provider: str
    pools_updated: int


class DisableProviderEnvKeyResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Response for ``POST /admin/provider-keys/disable-env``."""

    id: str
    provider: str
    pools_updated: int
