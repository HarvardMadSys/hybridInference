"""Application settings using Pydantic.

This module provides type-safe, validated configuration management.
All environment variables are centralized here for easy tracking and testing.
"""

from enum import Enum
from functools import lru_cache

from pydantic_settings import BaseSettings


class RoutingStrategy(Enum):
    """Available routing strategies."""

    FIXED = "fixed"
    NIMBUS = "nimbus"
    ROUTEWISE = "routewise"


class Settings(BaseSettings):
    """Application settings with validation and type safety."""

    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "freeinference_db"
    db_user: str = "postgres"
    db_password: str = ""

    # Database privacy settings
    db_store_full_content: bool = True

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
    login_rate_limit_per_15min: int = 5

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

    # ============================================
    # Routing Strategy Configuration
    # ============================================
    # Set to "nimbus" to enable SLO-aware intelligent routing
    # Set to "fixed" for traditional weighted routing
    routing_strategy: str = "nimbus"

    # Experiment mode for academic evaluation
    # When True: disables fallback, records all failures for clean experimental data
    # When False: enables fallback for production reliability
    experiment_mode: bool = False

    # Dry-run outsourcing: when True, outsourced requests return fake responses
    # instead of calling the remote API. Saves API quota during experiments.
    experiment_dry_run_outsource: bool = False

    # Nimbus: Models to enable hybrid routing for (legacy; prefer models.yaml routing_strategy)
    # These models MUST have both local (SGLang) and remote (API) adapters configured
    # NOTE: per-model routing_strategy in models.yaml takes precedence over this list.
    nimbus_enabled_models: list[str] = [
        # Example:
        "glm-4.6",
        "qwen3-coder-30b",
        # "minimax-m2",
    ]

    # Nimbus: Model-specific SLO thresholds (seconds)
    # Time-To-First-Token target for each model family
    glm46_slo_seconds: float = 2.0
    qwen3_slo_seconds: float = 1.5
    minimax_slo_seconds: float = 2.5

    def get_routing_strategy(self) -> RoutingStrategy:
        """Get the global routing strategy."""
        return RoutingStrategy(self.routing_strategy)

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
