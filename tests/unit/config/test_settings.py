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
from pydantic import ValidationError

import serving.config.settings as settings_module
from serving.config.settings import Settings, get_settings, get_signup_notify_emails, has_role

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
                    "REFRESH_",
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
    assert settings.refresh_token_cookie_name == "refresh_token"
    assert settings.db_host == "localhost"
    assert settings.db_port == 5432


def test_env_overrides_ignored_env_file(make_settings) -> None:
    """Environment variables should override defaults without using .env."""
    env = {
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": "30",
        "SIGNUP_ENABLED": "0",
        "DB_PORT": "5433",
        "REFRESH_TOKEN_COOKIE_NAME": "local_example_refresh",
    }
    settings = make_settings(env=env, env_file=None)
    assert settings.jwt_access_token_expire_minutes == 30
    assert settings.signup_enabled is False
    assert settings.db_port == 5433
    assert settings.refresh_token_cookie_name == "local_example_refresh"


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


def test_signup_notify_emails_falls_back_to_admin_emails(monkeypatch) -> None:
    """With no notify list set, recipients come from admin_emails.

    Casing is preserved for delivery: email local-parts may be case-sensitive.
    """
    monkeypatch.setattr(settings_module.settings, "admin_emails", "a@x.com, OpsMailbox@x.com")
    monkeypatch.setattr(settings_module.settings, "signup_notify_emails", "")
    assert get_signup_notify_emails() == ["a@x.com", "OpsMailbox@x.com"]


def test_signup_notify_emails_overrides_admin_emails(monkeypatch) -> None:
    """A configured notify list narrows recipients without touching admins."""
    monkeypatch.setattr(
        settings_module.settings, "admin_emails", "murphy@x.com,haoran@x.com,peter@x.com"
    )
    monkeypatch.setattr(settings_module.settings, "signup_notify_emails", "Peter@x.com")
    # Casing preserved for SMTP delivery; only the notify recipient is returned.
    assert get_signup_notify_emails() == ["Peter@x.com"]


def test_has_role_treats_unknown_user_role_as_lowest() -> None:
    """Unknown user roles should receive the lowest rank (free)."""
    assert has_role("typo-role", "free") is True
    assert has_role("typo-role", "pro") is False
    assert has_role("typo-role", "internal") is False


# =============================================================================
# Importing never validates; the recovery tools read the database settings alone
# =============================================================================


def test_importing_the_module_does_not_validate_the_environment() -> None:
    """The recovery CLIs import it, and must run while an unrelated value is invalid."""
    import subprocess
    import sys

    script = (
        "import serving.config.settings as m\n"
        "for read in (lambda: m.settings, m.DatabaseSettings):\n"
        "    try:\n"
        "        read()\n"
        "        print('read')\n"
        "    except Exception as exc:\n"
        "        print(type(exc).__name__)\n"
    )
    # A database connection setting is never set aside: reading it raises, as
    # it always has, though importing the module does not.
    env = {**os.environ, "DB_PORT": "not-a-port"}
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["ValidationError", "ValidationError"]


def test_an_invalid_environment_value_is_set_aside_until_the_database_answers(
    monkeypatch, caplog
) -> None:
    """Left at its default, reported, never counted as explicitly set."""
    monkeypatch.setenv("TRUSTED_PROXIES", "not-a-cidr")
    monkeypatch.setenv("SMTP_PORT", "not-a-number")
    monkeypatch.setenv("BASE_URL", "https://still.read")

    with caplog.at_level("WARNING", logger="serving.config.settings"):
        settings = get_settings()

    assert settings.trusted_proxies == ""
    assert settings.trusted_proxies_parsed == ()
    assert settings.smtp_port == Settings.model_fields["smtp_port"].default
    assert settings.base_url == "https://still.read"
    assert {"trusted_proxies", "smtp_port"}.isdisjoint(settings.model_fields_set)
    invalid = settings_module.invalid_environment()
    assert set(invalid) == {"trusted_proxies", "smtp_port"}
    assert "not a valid CIDR range" in invalid["trusted_proxies"]
    assert "TRUSTED_PROXIES in the environment is not valid" in caplog.text
    # Settings itself is as strict as ever.
    with pytest.raises(ValueError, match="trusted_proxies entry"):
        Settings()


def test_a_rule_between_settings_still_raises(monkeypatch) -> None:
    """No single value can be blamed, so nothing is set aside."""
    monkeypatch.setenv("TRUST_CLOUDFLARE_HEADERS", "1")
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

    with pytest.raises(ValueError, match="TRUST_CLOUDFLARE_HEADERS requires TRUST_PROXY_HEADERS"):
        get_settings()


@pytest.mark.parametrize(
    "environment,set_aside",
    [
        (
            {
                "TRUST_PROXY_HEADERS": "true",
                "TRUST_CLOUDFLARE_HEADERS": "true",
                "TRUSTED_CLOUDFLARE_NETWORKS": "not-a-cidr",
            },
            "trusted_cloudflare_networks",
        ),
        ({"TRUST_PROXY_HEADERS": "maybe", "TRUST_X_REAL_IP": "true"}, "trust_proxy_headers"),
    ],
)
def test_a_rule_on_a_set_aside_setting_waits_for_the_database(
    monkeypatch, environment, set_aside
) -> None:
    """Its default stands in for a value nobody chose, so the rule cannot judge it yet."""
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    settings = get_settings()

    assert set(settings_module.invalid_environment()) == {set_aside}
    # Nothing is trusted on the strength of the stand-in.
    assert settings.trusted_cloudflare_parsed == ()
    # The rule applies as before to every build outside get_settings().
    with pytest.raises(ValueError, match="requires"):
        Settings(
            **{
                settings_module.settings_init_name(set_aside): Settings.model_fields[
                    set_aside
                ].get_default()
            }
        )


def test_a_rule_on_settings_left_as_they_are_still_raises(monkeypatch) -> None:
    """Setting another value aside does not excuse a rule it plays no part in."""
    monkeypatch.setenv("SMTP_PORT", "not-a-number")
    monkeypatch.setenv("TRUST_X_REAL_IP", "1")
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

    # Raised with the environment's own error, as any refused environment is.
    with pytest.raises(ValidationError, match="smtp_port"):
        get_settings()
    # The rule is what refused it: without it, SMTP_PORT alone is set aside.
    monkeypatch.delenv("TRUST_X_REAL_IP")
    get_settings.cache_clear()
    assert set(settings_module.invalid_environment()) == {"smtp_port"}


def test_the_environment_validates_as_it_is_when_it_can(monkeypatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8")

    assert get_settings().trusted_proxies == "10.0.0.0/8"
    assert settings_module.invalid_environment() == {}


def test_the_module_global_is_created_once() -> None:
    """One object for the process, as when it was created at import."""
    shared = settings_module.settings
    get_settings.cache_clear()

    assert settings_module.settings is shared
    from serving.config.settings import settings

    assert settings is shared
    with pytest.raises(AttributeError):
        settings_module.no_such_attribute  # noqa: B018


def test_database_settings_read_only_the_connection(monkeypatch) -> None:
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_PORT", "6543")
    monkeypatch.setenv("db_name", "lower-case-works-too")
    monkeypatch.setenv("TRUSTED_PROXIES", "not-a-network")

    database = settings_module.DatabaseSettings()

    assert (database.db_host, database.db_port, database.db_name) == (
        "db.internal",
        6543,
        "lower-case-works-too",
    )
    assert database.db_user == Settings.model_fields["db_user"].default
    with pytest.raises(ValueError, match="trusted_proxies"):
        Settings()
