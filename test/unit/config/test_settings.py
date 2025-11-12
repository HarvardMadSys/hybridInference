"""Unit tests for configuration management."""

import os
import pytest

from serving.config.settings import Settings, get_settings


class TestSettingsValidation:
    """Test settings validation."""

    def test_settings_load_from_env(self, monkeypatch):
        """Test settings load from environment variables."""
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-chars-long!!")
        monkeypatch.setenv("API_KEY_SECRET", "test-api-key-secret")
        monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
        monkeypatch.setenv("DB_HOST", "localhost")
        monkeypatch.setenv("DB_NAME", "test_db")
        monkeypatch.setenv("DB_USER", "postgres")
        monkeypatch.setenv("DB_PASSWORD", "password")
        
        settings = Settings()
        
        assert settings.jwt_secret_key == "test-secret-key-32-chars-long!!"
        assert settings.api_key_secret == "test-api-key-secret"
        assert settings.admin_token == "test-admin-token"

    def test_settings_default_values(self, monkeypatch):
        """Test settings use correct default values."""
        # Clear all env vars to test defaults
        for key in list(os.environ.keys()):
            if key.startswith(("JWT_", "DB_", "SIGNUP_", "COOKIE_")):
                monkeypatch.delenv(key, raising=False)
        
        settings = Settings()
        
        # Check defaults (from actual implementation)
        assert settings.jwt_algorithm == "HS256"
        assert settings.jwt_access_token_expire_minutes == 15
        assert settings.jwt_refresh_token_expire_days == 30
        assert settings.signup_enabled is True
        assert settings.signup_default_daily_quota_usd == 100.00  # float, not Decimal
        assert settings.cookie_secure is False
        assert settings.cookie_samesite == "lax"  # lowercase
        assert settings.db_host == "localhost"
        assert settings.db_port == 5432

    def test_settings_type_conversion(self, monkeypatch):
        """Test settings correctly convert types."""
        monkeypatch.setenv("SIGNUP_ENABLED", "0")  # String "0"
        monkeypatch.setenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30")  # String "30"
        monkeypatch.setenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "50.00")  # String "50.00"
        monkeypatch.setenv("DB_PORT", "5433")  # String "5433"
        
        settings = Settings()
        
        # Check type conversions
        assert settings.signup_enabled is False  # bool
        assert settings.jwt_access_token_expire_minutes == 30  # int
        assert settings.signup_default_daily_quota_usd == 50.00  # float
        assert settings.db_port == 5433  # int

    def test_settings_case_insensitive(self, monkeypatch):
        """Test settings are case-insensitive."""
        monkeypatch.setenv("jwt_secret_key", "test-secret-key")  # lowercase
        monkeypatch.setenv("API_KEY_SECRET", "test-api-key-secret")  # uppercase
        
        settings = Settings()
        
        assert settings.jwt_secret_key == "test-secret-key"
        assert settings.api_key_secret == "test-api-key-secret"

    def test_settings_extra_fields_ignored(self, monkeypatch):
        """Test extra environment variables are ignored."""
        monkeypatch.setenv("UNKNOWN_FIELD", "some_value")
        
        # Should not raise error
        settings = Settings()
        assert not hasattr(settings, "unknown_field")


class TestSettingsUsage:
    """Test settings usage patterns."""

    def test_settings_singleton_pattern(self):
        """Test settings can be imported as singleton."""
        from serving.config.settings import settings
        
        # Should be accessible
        assert hasattr(settings, "jwt_secret_key")
        assert hasattr(settings, "db_host")

    def test_get_settings_cached(self):
        """Test get_settings returns cached instance."""
        settings1 = get_settings()
        settings2 = get_settings()
        
        # Should be same instance (cached)
        assert settings1 is settings2
