"""Application settings using Pydantic.

This module provides type-safe, validated configuration management.
All environment variables are centralized here for easy tracking and testing.
"""

from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

# Resource models a provider route can be added under. ``quota`` and
# ``concurrency`` are the stateful kinds RouteWise accounts for; ``on_demand``
# is plain pay-as-you-go.
ROUTE_TYPE_ORDER: tuple[str, ...] = ("on_demand", "quota", "concurrency")
ROUTE_TYPES: frozenset[str] = frozenset(ROUTE_TYPE_ORDER)


def parse_provider_route_types(raw: str) -> dict[str, frozenset[str]]:
    """Parse ``PROVIDER_ROUTE_TYPES`` into ``{provider: allowed route types}``.

    The value is a comma-separated list of ``provider=type[|type]`` entries. A
    malformed entry or an unknown route type raises ``ValueError`` naming it,
    so a typo fails startup instead of silently leaving that provider
    unrestricted.
    """
    policy: dict[str, frozenset[str]] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        provider, sep, types = entry.partition("=")
        provider = provider.strip()
        allowed = frozenset(kind.strip() for kind in types.split("|") if kind.strip())
        if not sep or not provider or not allowed:
            raise ValueError(
                f"PROVIDER_ROUTE_TYPES entry {entry!r} must look like "
                "provider=type or provider=type|type"
            )
        unknown = sorted(allowed - ROUTE_TYPES)
        if unknown:
            raise ValueError(
                f"PROVIDER_ROUTE_TYPES entry {entry!r}: unknown route type "
                f"{', '.join(unknown)}; expected one of {', '.join(ROUTE_TYPE_ORDER)}"
            )
        policy[provider] = allowed
    return policy


class Settings(BaseSettings):
    """Application settings with validation and type safety."""

    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "hybridinference"
    db_user: str = "postgres"
    db_password: str = ""

    # Database privacy settings
    # Default False: by default we hash prompt/response content rather than
    # storing it verbatim. Operators can opt in to full-content logging by
    # setting DB_STORE_FULL_CONTENT=true after weighing the privacy impact.
    db_store_full_content: bool = False

    # Admin
    admin_token: str = ""
    admin_emails: str = ""
    # Recipients for signup/registration approval notifications. Comma-separated.
    # When empty, falls back to admin_emails so existing deployments are
    # unaffected. Set this to notify a subset of admins (or a shared inbox)
    # without changing who holds the admin role.
    signup_notify_emails: str = ""
    user_auth_enabled: bool = True
    api_key_secret: str = ""

    # JWT (required in production)
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 30

    # Cookie
    cookie_secure: bool = True
    cookie_domain: str | None = None
    cookie_samesite: str = "lax"
    # Host-only cookies are still shared by every port on the same hostname.
    # A second local deployment can choose a distinct name without changing
    # the production-compatible default.
    refresh_token_cookie_name: str = "refresh_token"

    # Signup
    signup_enabled: bool = True
    # Deprecated: only used as fallback when no user_daily_quota_<role>
    # runtime setting is registered. Per-role quotas (user_daily_quota_free,
    # _pro, _internal, _admin) are the source of truth at signup.
    signup_default_daily_quota_usd: float = 100.00
    signup_require_email_verification: bool = True
    # Email the configured recipients when a new user registers and needs
    # approval. Set SIGNUP_ADMIN_NOTIFY_ENABLED=false to suppress these.
    signup_admin_notify_enabled: bool = True

    # Rate limiting
    signup_rate_limit_per_hour: int = 5
    signup_rate_limit_per_day: int = 10
    # Per-email window (default 5 attempts / 15 min) and per-IP window
    # (default 20 attempts / hour). The login limiter records attempts on
    # entry so probing varied passwords cannot bypass the limit.
    login_rate_limit_per_15min: int = 5
    login_rate_limit_per_hour_per_ip: int = 20
    # Auto-block a source IP at the API-key auth layer after repeated auth
    # failures. Once an IP (IPv6 bucketed to /64) reaches
    # auth_failure_block_threshold failures within auth_failure_block_window_sec,
    # it is refused for auth_failure_block_duration_sec. In-memory and
    # per-process, like the login/signup limiters above. Defaults: 200 failures
    # in a day → blocked for a day.
    auth_failure_block_enabled: bool = True
    auth_failure_block_threshold: int = 200
    auth_failure_block_window_sec: int = 86400
    auth_failure_block_duration_sec: int = 86400
    # Comma-separated IPs or CIDR ranges (e.g. "140.247.173.97,128.103.0.0/16")
    # that are exempt from auth-failure blocking: their failures are never
    # counted and an existing block never applies to them. For trusted shared
    # egress points (campus NAT, office gateways) where one client's stale key
    # would otherwise take every user behind the IP offline. Entries that fail
    # to parse are logged and skipped.
    auth_failure_block_exempt_ips: str = ""
    # Resolve who a rejected API key belongs to when auth fails, so the
    # ``auth_failure`` log record (and any alert built from it) can name the
    # account instead of only a count and an address. Worth having because the
    # keys that fail here are either nobody's -- a scanner's random token -- or a
    # deployment's own monitor, CI job or service account whose credential was
    # rotated, revoked or expired, and only the second is something to go and
    # fix. Costs one indexed lookup per failure, under the shared rejection
    # enrichment budget (REJECTED_ENRICHMENT_MAX_CONCURRENT), so a flood sheds it
    # instantly rather than queueing; the unbounded key lookup that already runs
    # on this path is the larger cost of the two. Set false to spend nothing.
    auth_failure_identify_caller: bool = True

    # Cloudflare Turnstile (signup captcha)
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""

    # Email (optional)
    smtp_host: str = "smtp.resend.com"
    smtp_port: int = 587
    smtp_user: str = "resend"
    smtp_password: str = ""
    smtp_from_email: str = "noreply@localhost"
    smtp_from_name: str = "HybridInference"

    # Public base URL of this gateway. Empty by default: an unconfigured
    # deployment has none, and the two consumers already handle that —
    # auth derives it from the request, and alerts treat an unset value as a
    # local run rather than claiming an environment.
    base_url: str = ""

    # Frontend URL (for email links). Must be absolute to be clickable in an
    # email, so the default points at a local console rather than being empty.
    frontend_url: str = "http://localhost:3001"

    # Qdrant (shared vector database for codebase indexing)
    qdrant_base_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""

    # CORS
    cors_allowed_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3002",
        "https://localhost:8443",
        "https://127.0.0.1:8443",
    ]

    # Trusted proxies (for real IP detection)
    trusted_proxies: list[str] = []

    # Route types the admin console may add per provider, as comma-separated
    # "provider=type[|type]" entries, e.g.
    # "chutes=quota,featherless=concurrency,openrouter=concurrency|on_demand".
    # This is a deployment's contract with its vendors — a request-quota plan
    # here, a concurrency plan there — so it lives here rather than in the
    # code. A provider that is not listed, or an empty value (the default),
    # may be added as any route type.
    provider_route_types: str = ""

    # Enable RouteWise online routing subsystem (per-model opt-in via models.yaml)
    enable_routewise: bool = False

    # Adaptive cap on the gateway's own outbound concurrency against one remote
    # account (serving.adapters.upstream_limiter). One bucket per
    # (provider label, API key); local inference servers are never limited.
    # The limit starts at _initial, drops by one on every upstream 429, and
    # probes upward by one every _probe_success_interval *successful (HTTP 200)
    # responses*, staying within [1, _max]. Only a 200 advances that counter —
    # an error is not evidence the provider has headroom. A request that finds
    # its bucket full waits up to _acquire_timeout_sec for a slot before failing
    # over to another endpoint, so the value should stay well under the
    # client-facing request timeout.
    upstream_concurrency_enabled: bool = True
    upstream_concurrency_initial_limit: int = Field(default=8, ge=1)
    upstream_concurrency_max_limit: int = Field(default=64, ge=1)
    upstream_concurrency_probe_success_interval: int = Field(default=100, ge=1)
    upstream_concurrency_acquire_timeout_sec: float = Field(default=30.0, gt=0.0)

    # Slack alerting (optional). Empty SLACK_WEBHOOK_URL disables the feature
    # entirely — no scheduler job is registered and no errors are raised.
    slack_webhook_url: str = ""
    failed_request_alert_threshold: int = Field(default=200, ge=0)
    failed_request_alert_window_minutes: int = Field(default=5, ge=1)
    failed_request_alert_cooldown_minutes: int = Field(default=5, ge=0)
    # Failing fraction of a window that alerts on its own, independent of the
    # absolute threshold above. That threshold is ~57,600 failures/day at its
    # defaults, so a sustained low-rate defect never reaches it -- the routing
    # IndexError in #1361 ran at ~1% of all traffic for 18 days and never fired.
    # 0.0 disables the rate rule. The default is deliberately well above the
    # current production baseline so merging this pages nobody; tighten it (0.02
    # is a reasonable target) once the standing failure sources are cleared.
    failed_request_alert_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    failed_request_alert_rate_min_count: int = Field(default=10, ge=0)

    # Config file paths. Canonical env names are MODELS_CONFIG_PATH /
    # ROUTING_CONFIG_PATH; the legacy MODELS_CONFIG / ROUTING_CONFIG names stay
    # honored, with the canonical name winning when both are set. The field
    # name itself is the last alias so Settings(models_config_path=...) works
    # in tests. Empty string means "not configured": callers warn when an
    # explicit override points at a missing file, but skip the config/*.yaml
    # defaults silently.
    models_config_path: str = Field(
        default="",
        validation_alias=AliasChoices("MODELS_CONFIG_PATH", "MODELS_CONFIG", "models_config_path"),
    )
    routing_config_path: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ROUTING_CONFIG_PATH", "ROUTING_CONFIG", "routing_config_path"
        ),
    )
    # Distribution manifest (serving.config.distribution). Empty path = pure
    # legacy behavior. Mode "dark" (the default) loads and validates the
    # manifest and logs what would change while current resolution stays
    # effective; applying manifest paths requires an explicit
    # DISTRIBUTION_CONFIG_MODE=active. Values are case-insensitive; anything
    # else degrades to "dark" with a warning — neither a typo nor a missing
    # mode can ever activate the manifest.
    distribution_config_path: str = Field(
        default="",
        validation_alias=AliasChoices("DISTRIBUTION_CONFIG_PATH", "distribution_config_path"),
    )
    distribution_config_mode: str = Field(
        default="dark",
        validation_alias=AliasChoices("DISTRIBUTION_CONFIG_MODE", "distribution_config_mode"),
    )

    # Alerting framework
    alerts_enabled: bool = Field(default=False, alias="ALERTS_ENABLED")
    slack_alerts_webhook_url: str = Field(default="", alias="SLACK_ALERTS_WEBHOOK_URL")
    alerts_config_path: str = Field(
        default="config/alerts.yaml",
        validation_alias=AliasChoices("ALERTS_CONFIG_PATH", "alerts_config_path"),
    )

    @model_validator(mode="after")
    def _alerts_webhook_fallback(self) -> "Settings":
        """Fall back to existing SLACK_WEBHOOK_URL when SLACK_ALERTS_WEBHOOK_URL unset."""
        if not self.slack_alerts_webhook_url and self.slack_webhook_url:
            object.__setattr__(self, "slack_alerts_webhook_url", self.slack_webhook_url)
        return self

    # Debug: when True, log full request payloads (including user prompts) at
    # DEBUG level in the OpenAI-compatible adapter. Defaults to False to avoid
    # leaking user content into logs in production.
    log_full_payload: bool = False

    class Config:
        """Pydantic configuration for Settings class."""

        env_file = ".env"
        case_sensitive = False
        # Allow extra fields for forward compatibility
        extra = "ignore"

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_allowed_origins(cls, value):
        """Accept either a list or a comma-separated env var for CORS origins."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("provider_route_types")
    @classmethod
    def validate_provider_route_types(cls, value: str) -> str:
        """Reject a route-type policy the admin console could not enforce."""
        parse_provider_route_types(value)
        return value

    def validate_auth_secrets(
        self, *, database_enabled: bool, user_auth_enabled: bool | None = None
    ) -> None:
        """Reject missing secrets before starting or enabling authentication.

        Database-backed deployments expose login and API-key management even
        when inference API-key authentication is disabled. Only a database-free,
        explicitly auth-disabled gateway can run without either secret.

        Raises:
            ValueError: If a required secret is empty or whitespace-only. The
                message names missing settings, never their values.
        """
        auth_enabled = self.user_auth_enabled if user_auth_enabled is None else user_auth_enabled
        if not database_enabled and not auth_enabled:
            return

        missing = [
            name
            for name, value in (
                ("JWT_SECRET_KEY", self.jwt_secret_key),
                ("API_KEY_SECRET", self.api_key_secret),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                "Authentication configuration incomplete: set "
                + ", ".join(missing)
                + " to non-blank values before starting the gateway or enabling user authentication."
            )


@lru_cache
def get_settings() -> Settings:
    """Get settings instance.

    This function is cached to return the same instance across the application.
    For testing, use pytest's monkeypatch or clear the cache with get_settings.cache_clear().

    Returns:
        Settings: Validated settings object.

    Raises:
        ValidationError: If required settings are missing or invalid.
    """
    return Settings()


# Global settings instance
# Note: This is created at import time. Tests should use get_settings() or
# reload the module to pick up environment changes.
settings = get_settings()


def _split_email_list(raw: str) -> list[str]:
    """Split a comma-separated email string, trimming whitespace and blanks.

    Case is preserved: email local-parts may be case-sensitive, so callers that
    deliver mail must not lowercase. Membership checks should lowercase
    separately via :func:`_parse_admin_emails`.
    """
    if not raw:
        return []
    return [e.strip() for e in raw.split(",") if e.strip()]


def _parse_admin_emails(raw: str) -> list[str]:
    """Parse comma-separated admin emails string into a lowercase list."""
    return [e.lower() for e in _split_email_list(raw)]


def is_admin_email(email: str) -> bool:
    """Check if the given email is in the admin list."""
    return email.strip().lower() in _parse_admin_emails(settings.admin_emails)


def get_signup_notify_emails() -> list[str]:
    """Return recipients for signup approval notifications.

    Uses ``signup_notify_emails`` when set, otherwise falls back to
    ``admin_emails`` so that recipients can be narrowed without altering who
    holds the admin role. Recipient casing is preserved for SMTP delivery.
    """
    notify = _split_email_list(settings.signup_notify_emails)
    if notify:
        return notify
    return _split_email_list(settings.admin_emails)


ROLE_RANK: dict[str, int] = {"free": 0, "pro": 1, "internal": 2, "admin": 3}

VALID_ROLES = frozenset(ROLE_RANK)


def has_role(user_role: str, required: str) -> bool:
    """Check if user_role meets or exceeds the required role level.

    Fail-closed: unknown *required* role is treated as rank infinity (never passes).
    Unknown *user_role* is treated as rank 0 (free).
    """
    if required not in ROLE_RANK:
        return False
    return ROLE_RANK.get(user_role, 0) >= ROLE_RANK[required]
