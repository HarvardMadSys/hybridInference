"""Contract freeze: deployment-identity settings defaults.

Characterization tests pinning the CURRENT default values of the
site-specific settings that the neutral-upstream / distribution split
(docs/agents/specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)
will parameterize. The branding/DistributionConfig PRs are expected to update
these assertions deliberately; an unexpected failure here means a refactor
changed a production default by accident.
"""

import pytest

from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY
from serving.config.settings import Settings

_SITE_ENV_VARS = (
    "BASE_URL",
    "FRONTEND_URL",
    "SMTP_FROM_EMAIL",
    "SMTP_FROM_NAME",
    "DB_NAME",
    "CORS_ALLOWED_ORIGINS",
    "ALERTS_CONFIG_PATH",
    "MODELS_CONFIG_PATH",
    "MODELS_CONFIG",
    "ROUTING_CONFIG_PATH",
    "ROUTING_CONFIG",
)


@pytest.fixture
def settings(monkeypatch) -> Settings:
    """Settings built from code defaults only (no env vars, no dotenv)."""
    for var in _SITE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return Settings(_env_file=None)


def test_site_identity_defaults(settings):
    """Deliberate neutral flip: the defaults name no distribution.

    A deployment's values moved to deploy/docker/docker-compose.yml, which
    pins each of these for the backend, so its deployments are unchanged.
    """
    # Empty: an unconfigured gateway has no public URL. Auth derives one from
    # the request and alerts treat "unset" as a local run, so nothing needs a
    # placeholder here.
    assert settings.base_url == ""
    # Absolute, because it is embedded in email links.
    assert settings.frontend_url == "http://localhost:3001"
    assert settings.smtp_from_email == "noreply@localhost"
    assert settings.smtp_from_name == "HybridInference"
    assert settings.db_name == "hybridinference"


def test_cors_default_origins(settings):
    """Only local origins ship by default; a site adds its own."""
    assert settings.cors_allowed_origins == [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3002",
        "https://localhost:8443",
        "https://127.0.0.1:8443",
    ]
    assert not any("freeinference" in origin for origin in settings.cors_allowed_origins)


def test_config_path_defaults(settings):
    assert settings.alerts_config_path == "config/alerts.yaml"


def test_role_concurrency_defaults():
    registry = RUNTIME_SETTINGS_REGISTRY
    assert registry["user_concurrency_free"]["default"] == 3
    assert registry["user_concurrency_pro"]["default"] == 3
    assert registry["user_concurrency_internal"]["default"] == 10
    assert registry["user_concurrency_admin"]["default"] == 10


def test_role_daily_quota_defaults():
    registry = RUNTIME_SETTINGS_REGISTRY
    assert registry["user_daily_quota_free"]["default"] == 100.00
    assert registry["user_daily_quota_pro"]["default"] == 100.00
    assert registry["user_daily_quota_internal"]["default"] == 1000.00
    assert registry["user_daily_quota_admin"]["default"] == 1000.00


def test_the_auth_503_names_the_right_remedy(monkeypatch) -> None:
    """Two states reach the same code path and need opposite advice.

    A deployment that never configured a database is told how to configure one.
    A deployment whose database is configured but was unreachable at startup
    reaches the identical `op_store is None`, and telling it to set
    DB_ENABLED=true sends the operator to check a setting that is already
    right, instead of at the database.
    """
    from serving.servers.deps import auth_database_detail, database_enabled

    monkeypatch.setenv("DB_ENABLED", "false")
    assert not database_enabled()
    assert "none is configured" in auth_database_detail()

    monkeypatch.setenv("DB_ENABLED", "true")
    assert database_enabled()
    assert "could not be reached" in auth_database_detail()

    # The default is on, so an unset variable is the configured case.
    monkeypatch.delenv("DB_ENABLED", raising=False)
    assert database_enabled()
