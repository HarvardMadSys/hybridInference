"""Application settings using Pydantic.

This module provides type-safe, validated configuration management.
All environment variables are centralized here for easy tracking and testing.
"""

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    """Application settings with validation and type safety."""

    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "freeinference_db"
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

    # Signup
    signup_enabled: bool = True
    # Deprecated: only used as fallback when no user_daily_quota_<role>
    # runtime setting is registered. Per-role quotas (user_daily_quota_free,
    # _pro, _internal, _admin) are the source of truth at signup.
    signup_default_daily_quota_usd: float = 100.00
    signup_require_email_verification: bool = True

    # Rate limiting
    signup_rate_limit_per_hour: int = 5
    signup_rate_limit_per_day: int = 10
    # Per-email window (default 5 attempts / 15 min) and per-IP window
    # (default 20 attempts / hour). The login limiter records attempts on
    # entry so probing varied passwords cannot bypass the limit.
    login_rate_limit_per_15min: int = 5
    login_rate_limit_per_hour_per_ip: int = 20

    # Cloudflare Turnstile (signup captcha)
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""

    # Email (optional)
    smtp_host: str = "smtp.resend.com"
    smtp_port: int = 587
    smtp_user: str = "resend"
    smtp_password: str = ""
    smtp_from_email: str = "noreply@freeinference.org"
    smtp_from_name: str = "FreeInference"

    # Base URL
    base_url: str = "https://freeinference.org"

    # Frontend URL (for email links)
    frontend_url: str = "https://freeinference.org"

    # Qdrant (shared vector database for codebase indexing)
    qdrant_base_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""

    # Provider quota cookies (admin dashboard "Providers" tab)
    # Pasted from browser DevTools after logging into the provider's web dashboard.
    # Re-paste when the cookie expires.
    # MiniMax: prefer MINIMAX_API_KEY (Bearer auth, no expiry); this cookie is a
    # legacy fallback used only when no API key is configured.
    minimax_session_cookie: str = ""
    minimax_group_id: str = ""
    ollama_session_cookie: str = ""

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
        "http://freeinference.org",
        "http://freeinference.org:3001",
        "https://freeinference.org",
        "https://freeinference.org:3001",
        "http://staging-internal.freeinference.org",
        "https://staging-internal.freeinference.org",
    ]

    # Trusted proxies (for real IP detection)
    trusted_proxies: list[str] = []

    # Enable RouteWise online routing subsystem (per-model opt-in via models.yaml)
    enable_routewise: bool = False

    # Slack alerting (optional). Empty SLACK_WEBHOOK_URL disables the feature
    # entirely — no scheduler job is registered and no errors are raised.
    slack_webhook_url: str = ""
    failed_request_alert_threshold: int = Field(default=20, ge=0)
    failed_request_alert_window_minutes: int = Field(default=5, ge=1)
    failed_request_alert_cooldown_minutes: int = Field(default=5, ge=0)

    # Alerting framework (replaces Prometheus)
    alerts_enabled: bool = Field(default=False, alias="ALERTS_ENABLED")
    slack_alerts_webhook_url: str = Field(default="", alias="SLACK_ALERTS_WEBHOOK_URL")
    alerts_config_path: str = Field(default="config/alerts.yaml", alias="ALERTS_CONFIG_PATH")

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
