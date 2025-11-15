"""Application settings using Pydantic.

This module provides type-safe, validated configuration management.
All environment variables are centralized here for easy tracking and testing.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings with validation and type safety."""

    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "freeinference_db"
    db_user: str = "postgres"
    db_password: str = ""

    # Admin
    admin_token: str = ""
    user_auth_enabled: bool = True
    api_key_secret: str = ""

    # JWT (required in production)
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 30

    # Cookie
    cookie_secure: bool = False
    cookie_domain: str | None = None
    cookie_samesite: str = "lax"

    # Signup
    signup_enabled: bool = True
    signup_default_tier: str = "free"
    signup_default_daily_quota_usd: float = 100.00
    signup_require_email_verification: bool = True

    # Rate limiting
    signup_rate_limit_per_hour: int = 5
    login_rate_limit_per_15min: int = 5

    # Email (optional)
    smtp_host: str = "smtp.resend.com"
    smtp_port: int = 587
    smtp_user: str = "resend"
    smtp_password: str = ""
    smtp_from_email: str = "noreply@freeinference.org"
    smtp_from_name: str = "FreeInference"

    # Base URL
    base_url: str = "http://freeinference.org"

    # Frontend URL (for email links)
    frontend_url: str = "http://freeinference.org:3001"

    # CORS
    cors_allowed_origins: list[str] = [
        "http://localhost:3000",
        "http://freeinference.org",
        "http://freeinference.org:3001",
        "https://freeinference.org",
        "https://freeinference.org:3001",
    ]

    # Trusted proxies (for real IP detection)
    trusted_proxies: list[str] = []

    class Config:
        """Pydantic configuration for Settings class."""

        env_file = ".env"
        case_sensitive = False
        # Allow extra fields for forward compatibility
        extra = "ignore"


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
