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
    assert settings.base_url == "https://freeinference.org"
    assert settings.frontend_url == "https://freeinference.org"
    assert settings.smtp_from_email == "noreply@freeinference.org"
    assert settings.smtp_from_name == "FreeInference"
    assert settings.db_name == "freeinference_db"


def test_cors_default_origins(settings):
    assert settings.cors_allowed_origins == [
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
