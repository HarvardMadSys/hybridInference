"""Registry-shape tests for runtime settings."""

from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY


def test_per_role_daily_quota_keys_registered():
    keys = {
        "user_daily_quota_free",
        "user_daily_quota_pro",
        "user_daily_quota_internal",
        "user_daily_quota_admin",
    }
    assert keys.issubset(RUNTIME_SETTINGS_REGISTRY.keys())


def test_per_role_daily_quota_entries_are_well_formed():
    for role, expected_default in (
        ("free", 100.00),
        ("pro", 100.00),
        ("internal", 1000.00),
        ("admin", 1000.00),
    ):
        entry = RUNTIME_SETTINGS_REGISTRY[f"user_daily_quota_{role}"]
        assert entry["type"] == "float"
        assert entry["default"] == expected_default
        assert entry["min"] == 0.0
        assert entry.get("description")


def test_per_role_concurrency_keys_registered():
    keys = {
        "user_concurrency_free",
        "user_concurrency_pro",
        "user_concurrency_internal",
        "user_concurrency_admin",
    }
    assert keys.issubset(RUNTIME_SETTINGS_REGISTRY.keys())


def test_free_concurrency_default_is_three():
    entry = RUNTIME_SETTINGS_REGISTRY["user_concurrency_free"]
    assert entry["type"] == "int"
    assert entry["default"] == 3
    assert entry["min"] == 1


def test_force_chat_completions_streaming_registered():
    entry = RUNTIME_SETTINGS_REGISTRY["force_chat_completions_streaming"]
    assert entry["type"] == "bool"
    assert entry["default"] is False
    assert entry.get("description")
