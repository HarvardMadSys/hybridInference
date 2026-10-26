"""Batch inference settings: defaults, live overrides, and the role gate."""

from __future__ import annotations

import pytest

from serving.batch import config as batch_config_module
from serving.batch.config import BatchConfig, batch_config, may_use_batch


@pytest.fixture
def stored(monkeypatch):
    """Values the configuration resolver answers with, keyed like config_value()."""
    values: dict[str, str] = {}

    def fake_config_value(key: str, default: str | None = None) -> str | None:
        return values.get(key, default)

    monkeypatch.setattr(batch_config_module, "config_value", fake_config_value)
    return values


def test_defaults_restrict_batches_to_internal_users(stored):
    config = batch_config()

    assert config == BatchConfig()
    assert config.enabled is True
    assert config.min_role == "internal"
    assert config.default_model_concurrency == 64
    assert config.user_max_concurrency == 64
    assert config.role_concurrency == {"free": 8, "pro": 16, "internal": 64, "admin": 64}
    assert config.max_body_bytes == 32 * 1024 * 1024
    assert config.completion_window_seconds == 24 * 3600


def test_stored_values_apply_on_the_next_read(stored):
    stored.update(
        {
            "BATCH_ENABLED": "0",
            "BATCH_MIN_ROLE": "PRO",
            "BATCH_MODEL_CONCURRENCY": "0",
            "BATCH_YIELD_TO_INTERACTIVE": "0",
            "BATCH_MAX_REQUESTS": "100",
            "BATCH_MAX_BODY_MB": "2",
            "BATCH_MAX_ACTIVE_PER_USER": "3",
            "BATCH_COMPLETION_WINDOW_HOURS": "48",
            "BATCH_RETENTION_DAYS": "30",
            "BATCH_USER_MAX_CONCURRENCY": "20",
            "BATCH_ROLE_CONCURRENCY_INTERNAL": "12",
            "BATCH_ROLE_CONCURRENCY_ADMIN": "0",
        }
    )

    config = batch_config()

    assert config.enabled is False
    assert config.min_role == "pro"
    assert config.default_model_concurrency == 0
    assert config.yield_to_interactive is False
    assert config.max_requests == 100
    assert config.max_body_bytes == 2 * 1024 * 1024
    assert config.max_active_per_user == 3
    assert config.completion_window_hours == 48
    assert config.retention_days == 30
    assert config.user_max_concurrency == 20
    assert config.role_concurrency == {"free": 8, "pro": 16, "internal": 12, "admin": 0}


@pytest.mark.parametrize(
    ("key", "raw", "field", "expected"),
    [
        ("BATCH_MIN_ROLE", "superuser", "min_role", "internal"),
        ("BATCH_MODEL_CONCURRENCY", "-1", "default_model_concurrency", 64),
        ("BATCH_USER_MAX_CONCURRENCY", "-1", "user_max_concurrency", 64),
        ("BATCH_MAX_REQUESTS", "lots", "max_requests", 50_000),
        ("BATCH_MAX_REQUESTS", "0", "max_requests", 50_000),
        ("BATCH_COMPLETION_WINDOW_HOURS", "169", "completion_window_hours", 24),
        ("BATCH_ENABLED", "", "enabled", True),
    ],
)
def test_unusable_values_fall_back_to_the_default(stored, key, raw, field, expected):
    stored[key] = raw

    assert getattr(batch_config(), field) == expected


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("free", False), ("pro", False), ("internal", True), ("admin", True), (None, False)],
)
def test_only_internal_and_admin_may_create_by_default(role, allowed):
    assert may_use_batch(role, BatchConfig()) is allowed


def test_min_role_is_configurable():
    assert may_use_batch("pro", BatchConfig(min_role="pro")) is True
    assert may_use_batch("internal", BatchConfig(min_role="admin")) is False


def test_disabled_batches_admit_nobody():
    assert may_use_batch("admin", BatchConfig(enabled=False)) is False


def test_may_use_batch_reads_the_live_config_by_default(stored):
    stored["BATCH_MIN_ROLE"] = "free"

    assert may_use_batch("free") is True


def test_an_unusable_role_quota_falls_back_to_that_roles_default(stored):
    stored["BATCH_ROLE_CONCURRENCY_PRO"] = "-3"
    stored["BATCH_ROLE_CONCURRENCY_FREE"] = "many"

    assert batch_config().role_concurrency == {"free": 8, "pro": 16, "internal": 64, "admin": 64}
