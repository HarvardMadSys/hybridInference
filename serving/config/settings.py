"""Application settings using Pydantic.

This module provides type-safe, validated configuration management.
All environment variables are centralized here for easy tracking and testing.
"""

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    """Application settings with validation and type safety."""

    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "freeinference_db"
    db_user: str = "postgres"
    db_password: str = ""

    # Database backend: "postgres" (default) or "d1" (Cloudflare D1 for operational tables)
    db_backend: str = "postgres"
    # Dual-write: when DB_BACKEND=d1, also shadow-write to PostgreSQL as a warm standby
    db_dual_write: bool = False

    # Database privacy settings
    db_store_full_content: bool = True

    # Cloudflare D1 (used when db_backend = "d1")
    d1_account_id: str = ""
    d1_database_id: str = ""
    d1_api_token: str = ""

    # Cloudflare R2 (log archival — S3-compatible)
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "hybridinference-logs"
    r2_endpoint_url: str = ""  # e.g. https://<account_id>.r2.cloudflarestorage.com
    r2_log_retention_days: int = 30  # keep logs in D1 for this many days

    # Admin
    admin_token: str = ""
    admin_emails: str = ""
    user_auth_enabled: bool = True
    api_key_secret: str = ""

    # JWT (required in production)
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 365

    # Cookie
    cookie_secure: bool = True
    cookie_domain: str | None = None
    cookie_samesite: str = "lax"

    # Signup
    signup_enabled: bool = True
    signup_default_tier: str = "free"
    signup_default_daily_quota_usd: float = 100.00
    signup_require_email_verification: bool = True

    # Rate limiting
    signup_rate_limit_per_hour: int = 5
    signup_rate_limit_per_day: int = 10
    login_rate_limit_per_15min: int = 5

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

    # Codex subscription
    codex_accounts_file: str = "var/data/codex_accounts.json"
    codex_fallback_api_key: str = ""
    codex_token_refresh_margin: int = 30
    codex_account_cooldown: int = 60
    codex_failure_threshold: int = 3

    # Claude subscription
    claude_sub_accounts_file: str = "var/data/claude_accounts.json"
    claude_sub_fallback_api_key: str = ""
    claude_sub_token_refresh_margin: int = 300  # 5 min (tokens last ~1 hour)
    claude_sub_account_cooldown: int = 60
    claude_sub_failure_threshold: int = 3

    # Provider quota cookies (admin dashboard "Providers" tab)
    # Pasted from browser DevTools after logging into the provider's web dashboard.
    # Re-paste when the cookie expires.
    minimax_session_cookie: str = ""
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
    # Experiment mode: when True, disable fallback in BaseRouter for A/B testing
    experiment_mode: bool = False

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


def _parse_admin_emails(raw: str) -> list[str]:
    """Parse comma-separated admin emails string into a lowercase list."""
    if not raw:
        return []
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def is_admin_email(email: str) -> bool:
    """Check if the given email is in the admin list."""
    return email.strip().lower() in _parse_admin_emails(settings.admin_emails)


ROLE_RANK: dict[str, int] = {"free": 0, "pro": 1, "internal": 2, "admin": 3}

VALID_ROLES = frozenset(ROLE_RANK)

# Per-user concurrency caps by role. Used by serving/servers/concurrency.py.
USER_CONCURRENCY_LIMITS: dict[str, int] = {
    "free": 1,
    "pro": 3,
    "internal": 10,
    "admin": 10,
}


def has_role(user_role: str, required: str) -> bool:
    """Check if user_role meets or exceeds the required role level.

    Fail-closed: unknown *required* role is treated as rank infinity (never passes).
    Unknown *user_role* is treated as rank 0 (free).
    """
    if required not in ROLE_RANK:
        return False
    return ROLE_RANK.get(user_role, 0) >= ROLE_RANK[required]
