"""Unit tests for configuration management.

These tests validate Settings behavior across different configuration sources:
- class defaults
- environment variables
- dotenv files
- caching behavior
"""

import os
from collections.abc import Callable

import pytest

from serving.config.settings import Settings, get_settings, has_role

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    """Ensure settings cache is cleared before and after each test.

    This avoids interference between tests that rely on different env setups.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def make_settings(monkeypatch) -> Callable[[dict[str, str] | None, str | None], Settings]:
    """Factory to build Settings with controlled environment and dotenv.

    Args:
      monkeypatch: Pytest monkeypatch fixture.

    Returns:
      Callable that constructs a Settings object with given env and dotenv file.
    """

    def _build(env: dict[str, str] | None = None, env_file: str | None = None) -> Settings:
        # Clear relevant env keys to avoid leakage from previous tests or shell.
        for key in list(os.environ.keys()):
            if key.startswith(
                (
                    "JWT_",
                    "DB_",
                    "SIGNUP_",
                    "COOKIE_",
                    "SMTP_",
                    "CORS_",
                    "BASE_URL",
                )
            ):
                monkeypatch.delenv(key, raising=False)
        # Apply provided env overrides for this test.
        if env:
            for k, v in env.items():
                monkeypatch.setenv(k, v)
        # Build Settings with explicit dotenv selection.
        return Settings(_env_file=env_file)

    return _build


# =============================================================================
# Tests: Defaults and overrides
# =============================================================================


def test_defaults_without_env_file(make_settings) -> None:
    """Defaults should be used when no env and no .env file is provided."""
    settings = make_settings(env=None, env_file=None)
    assert settings.jwt_algorithm == "HS256"
    assert settings.jwt_access_token_expire_minutes == 15
    assert settings.jwt_refresh_token_expire_days == 30
    assert settings.signup_enabled is True
    assert settings.signup_default_daily_quota_usd == 100.00  # float, not Decimal
    # In production we default to secure cookies; override via env for local HTTP dev if needed.
    assert settings.cookie_secure is True
    assert settings.cookie_samesite == "lax"
    assert settings.db_host == "localhost"
    assert settings.db_port == 5432


def test_env_overrides_ignored_env_file(make_settings) -> None:
    """Environment variables should override defaults without using .env."""
    env = {
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "30",
        "SIGNUP_ENABLED": "0",
        "DB_PORT": "5433",
    }
    settings = make_settings(env=env, env_file=None)
    assert settings.jwt_access_token_expire_minutes == 30
    assert settings.signup_enabled is False
    assert settings.db_port == 5433


def test_env_precedence_env_over_dotenv(tmp_path, make_settings) -> None:
    """Env vars should take precedence over .env; .env over defaults."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("DB_PORT=5434\nJWT_ACCESS_TOKEN_EXPIRE_MINUTES=20\n")

    # Only .env provided.
    s1 = make_settings(env=None, env_file=str(dotenv))
    assert s1.db_port == 5434
    assert s1.jwt_access_token_expire_minutes == 20

    # Env overrides .env.
    s2 = make_settings(env={"DB_PORT": "5435"}, env_file=str(dotenv))
    assert s2.db_port == 5435
    # Other values still come from .env.
    assert s2.jwt_access_token_expire_minutes == 20


def test_case_insensitive_and_type_conversion(make_settings) -> None:
    """Settings should be case-insensitive and perform type conversions."""
    env = {
        "jwt_secret_key": "test-secret-key",  # lowercase key
        "SIGNUP_ENABLED": "0",  # string bool
        "DB_PORT": "5433",  # string int
        "SIGNUP_DEFAULT_DAILY_QUOTA_USD": "50.00",  # string float
    }
    settings = make_settings(env=env, env_file=None)
    assert settings.jwt_secret_key == "test-secret-key"
    assert settings.signup_enabled is False
    assert settings.db_port == 5433
    assert settings.signup_default_daily_quota_usd == 50.00


def test_settings_extra_fields_ignored(make_settings) -> None:
    """Unknown environment variables should be ignored."""
    settings = make_settings(env={"UNKNOWN_FIELD": "some_value"}, env_file=None)
    assert not hasattr(settings, "unknown_field")


def test_cors_allowed_origins_accepts_comma_separated_env(make_settings) -> None:
    """CORS origins should support simple comma-separated env overrides."""
    settings = make_settings(
        env={"CORS_ALLOWED_ORIGINS": "http://localhost:3002, https://staging.example.com"},
        env_file=None,
    )
    assert settings.cors_allowed_origins == [
        "http://localhost:3002",
        "https://staging.example.com",
    ]


# =============================================================================
# Tests: Caching and singleton behavior
# =============================================================================


def test_settings_singleton_pattern() -> None:
    """Settings singleton import should provide an object with expected attrs."""
    from serving.config.settings import settings as singleton

    assert hasattr(singleton, "jwt_secret_key")
    assert hasattr(singleton, "db_host")


def test_get_settings_cached() -> None:
    """get_settings should return the same cached instance within a test."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_has_role_enforces_rank_order() -> None:
    """has_role should honor the configured role hierarchy."""
    assert has_role("admin", "internal") is True
    assert has_role("internal", "internal") is True
    assert has_role("internal", "free") is True
    assert has_role("free", "internal") is False
    assert has_role("internal", "admin") is False


def test_has_role_fails_closed_for_unknown_required_role() -> None:
    """Unknown required roles should never be treated as allowed."""
    assert has_role("admin", "super_admin") is False


def test_has_role_treats_unknown_user_role_as_lowest() -> None:
    """Unknown user roles should receive the lowest rank (free)."""
    assert has_role("typo-role", "free") is True
    assert has_role("typo-role", "pro") is False
    assert has_role("typo-role", "internal") is False
