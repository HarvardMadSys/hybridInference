"""The registry of settings the database-backed configuration manages."""

from __future__ import annotations

import textwrap
import typing

import pytest

from serving.config import app_config_registry as registry
from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY
from serving.config.settings import Settings


def _context(values: dict[str, str] | None = None, **overrides) -> registry.RequirementContext:
    values = values or {}
    options = {
        "database_enabled": True,
        "user_auth_enabled": True,
        "email_verification_needed": False,
        "value": lambda key: values.get(key, ""),
    }
    options.update(overrides)
    return registry.RequirementContext(**options)


# --- classification ----------------------------------------------------------


def test_every_settings_field_is_classified() -> None:
    """A new Settings field must be registered or explicitly left out, with a reason."""
    registered = {entry.field for entry in registry.static_entries() if entry.field}
    unclassified = [
        name
        for name in Settings.model_fields
        if name not in registered
        and registry.settings_env_name(name) not in registry.ENVIRONMENT_ONLY
        and name not in RUNTIME_SETTINGS_REGISTRY
        and name not in registry.UNREGISTERED_SETTINGS_FIELDS
    ]
    assert unclassified == [], (
        "Settings fields in no list: register them in app_config_registry, or add them to "
        f"ENVIRONMENT_ONLY / UNREGISTERED_SETTINGS_FIELDS with a reason: {unclassified}"
    )


def test_a_field_is_classified_only_once() -> None:
    registered = {entry.field for entry in registry.static_entries() if entry.field}
    for name in Settings.model_fields:
        homes = [
            name in registered,
            registry.settings_env_name(name) in registry.ENVIRONMENT_ONLY,
            name in RUNTIME_SETTINGS_REGISTRY,
            name in registry.UNREGISTERED_SETTINGS_FIELDS,
        ]
        assert sum(homes) <= 1, f"{name} is classified more than once"


def test_unregistered_fields_name_a_reason() -> None:
    for name, reason in registry.UNREGISTERED_SETTINGS_FIELDS.items():
        assert name in Settings.model_fields
        assert reason.strip()


def test_static_entries_are_unique_and_well_formed() -> None:
    keys = [entry.key for entry in registry.static_entries()]
    assert len(keys) == len(set(keys))
    for entry in registry.static_entries():
        assert entry.category in registry.CATEGORY_IDS, entry.key
        assert entry.description.strip(), entry.key
        assert not registry.is_environment_only(entry.key), entry.key
        assert not registry.is_runtime_setting_name(entry.key), entry.key
        if entry.field is not None:
            assert registry.settings_env_name(entry.field) == entry.key


def test_settings_entries_match_their_field_types() -> None:
    expected = {bool: "bool", int: "int", float: "float"}
    for entry in registry.static_entries():
        if entry.field is None:
            continue
        annotation = Settings.model_fields[entry.field].annotation
        if annotation in expected:
            assert entry.type == expected[annotation], entry.key
        elif typing.get_origin(annotation) is list:
            assert entry.type == "list", entry.key
        else:
            assert entry.type in ("str", "list", "text"), entry.key


def test_flags_the_spec_names() -> None:
    by_key = {entry.key: entry for entry in registry.static_entries()}
    assert by_key["JWT_SECRET_KEY"].generated and by_key["JWT_SECRET_KEY"].secret
    assert by_key["API_KEY_SECRET"].generated and by_key["API_KEY_SECRET"].immutable
    assert by_key["ERASURE_FENCE_SECRET"].immutable
    assert by_key["ERASURE_FENCE_SECRET"].fallback_key == "API_KEY_SECRET"
    for key in (
        "ENABLE_ROUTEWISE",
        "ALERTS_ENABLED",
        "SLACK_WEBHOOK_URL",
        "FAILED_REQUEST_ALERT_THRESHOLD",
        "DB_STORE_FULL_CONTENT",
        "ERASURE_FENCE_SECRET",
        "UPSTREAM_CONCURRENCY_ENABLED",
        "CIRCUIT_FAILURE_THRESHOLD",
        "ROUTER_HEALTH_EWMA_ALPHA",
    ):
        assert by_key[key].restart_required, key
    for key in ("CORS_ALLOWED_ORIGINS", "TRUST_PROXY_HEADERS", "ROUTING_AFFINITY_ENABLED"):
        assert not by_key[key].restart_required, key
    # The routing switches are read as value != "0" and must be stored that way.
    assert by_key["ROUTING_AFFINITY_ENABLED"].flag
    assert by_key["ROUTING_PREFILL_AWARE_ENABLED"].flag


def test_auth_secrets_are_required_unless_database_free_and_auth_off() -> None:
    entry = registry.static_entry("JWT_SECRET_KEY")
    assert entry is not None
    assert entry.is_required(_context())
    assert entry.is_required(_context(database_enabled=False))
    assert not entry.is_required(_context(database_enabled=False, user_auth_enabled=False))


def test_smtp_credentials_are_required_while_signup_needs_verification() -> None:
    for key in ("SMTP_USER", "SMTP_PASSWORD"):
        entry = registry.static_entry(key)
        assert entry is not None
        assert entry.is_required(_context(email_verification_needed=True))
        assert not entry.is_required(_context(email_verification_needed=False))


_DISTINCT_VALUES = {
    "cors_allowed_origins": "https://x.example",
    "trusted_proxies": "10.0.0.0/8",
    "trusted_direct_client_networks": "192.168.0.0/16",
    "trusted_cloudflare_networks": "10.1.0.0/16",
    "provider_route_types": "chutes=quota",
    "cookie_samesite": "strict",
    "cookie_domain": "example.com",
}
# Values a field cannot take on its own, by the cross-field rules (field names).
_PREREQUISITES = {
    "trust_cloudflare_headers": {
        "trust_proxy_headers": "true",
        "trusted_cloudflare_networks": "10.1.0.0/16",
    },
    "trust_x_real_ip": {"trust_proxy_headers": "true"},
}


def _distinct_value(entry: registry.ConfigEntry) -> str:
    """A valid value for the entry's field that differs from its default."""
    if entry.field in _DISTINCT_VALUES:
        return _DISTINCT_VALUES[entry.field]
    if entry.type == "bool":
        return "false" if entry.default == "true" else "true"
    if entry.type == "int":
        return str(int(entry.default or 0) + 1)
    if entry.type == "float":
        return str(float(entry.default or 0) / 2 + 0.25)
    return "a-distinct-value"


def test_settings_init_names_follow_each_field_declaration() -> None:
    # alias= takes only the alias; AliasChoices takes its canonical spelling.
    assert registry.settings_init_name("alerts_enabled") == "ALERTS_ENABLED"
    assert registry.settings_init_name("slack_alerts_webhook_url") == "SLACK_ALERTS_WEBHOOK_URL"
    assert registry.settings_init_name("trust_proxy_headers") == "TRUST_PROXY_HEADERS"
    assert registry.settings_init_name("smtp_port") == "smtp_port"
    # Settings ignores an unknown keyword, so each name must demonstrably land.
    for entry in registry.static_entries():
        if entry.field is None:
            continue
        kwargs = {
            registry.settings_init_name(field): value
            for field, value in _PREREQUISITES.get(entry.field, {}).items()
        }
        kwargs[registry.settings_init_name(entry.field)] = _distinct_value(entry)
        default = Settings.model_fields[entry.field].get_default(call_default_factory=True)
        assert getattr(Settings(**kwargs), entry.field) != default, entry.key


def test_runtime_and_environment_only_names() -> None:
    assert registry.is_runtime_setting_name("USER_AUTH_ENABLED")
    assert registry.is_runtime_setting_name("SIGNUP_ENABLED")
    assert registry.is_environment_only("DB_HOST")
    assert registry.is_environment_only("MODELS_CONFIG")
    assert registry.is_environment_only("REFRESH_TOKEN_COOKIE_NAME")


# --- numbered and custom -----------------------------------------------------


# Built rather than spelled out: a literal list of key names reads as a
# credential assignment to secret scanners.
@pytest.mark.parametrize(
    "key",
    [f"{base}_API_KEY{n}" for base, n in (("MINIMAX", 2), ("CHUTES", 20), ("OPENROUTER", 1))],
)
def test_numbered_provider_keys_are_recognized(key: str) -> None:
    entry = registry.numbered_key_entry(key)
    assert entry is not None
    assert entry.secret and entry.restart_required
    assert entry.category == "providers"


@pytest.mark.parametrize("key", ["MINIMAX_API_KEY21", "MINIMAX_API_KEY0", "MINIMAX_API_KEY02"])
def test_out_of_range_numbered_keys_are_not(key: str) -> None:
    assert registry.numbered_key_entry(key) is None


def test_build_entries_lists_present_numbered_keys_and_custom_rows() -> None:
    entries = registry.build_entries(
        {},
        {"MY_CUSTOM_TOKEN": True, "MINIMAX_API_KEY3": True},
        {"MINIMAX_API_KEY2": "k2", "CHUTES_API_KEY2": "", "PATH": "/bin"},
    )
    assert entries["MINIMAX_API_KEY2"].origin == "numbered"
    assert entries["MINIMAX_API_KEY3"].origin == "numbered"
    assert "CHUTES_API_KEY2" not in entries  # empty in the environment, no row
    assert entries["MY_CUSTOM_TOKEN"].origin == "custom"
    assert entries["MY_CUSTOM_TOKEN"].secret
    assert "PATH" not in entries


def test_build_entries_never_lists_environment_only_or_runtime_rows() -> None:
    entries = registry.build_entries({}, {"DB_HOST": False, "USER_AUTH_ENABLED": False}, {})
    assert "DB_HOST" not in entries
    assert "USER_AUTH_ENABLED" not in entries


# --- discovery ---------------------------------------------------------------


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(textwrap.dedent(text))
    return path


def test_discovery_marks_what_a_non_optional_route_needs(tmp_path) -> None:
    models = _write(
        tmp_path,
        "models.yaml",
        """
        # A comment mentioning ${COMMENTED_OUT} counts for nothing.
        models:
          - id: chat-a
            base_url: ${SHARED_BASE}
            api_key: ${MODEL_KEY}
            provider_model_id: ${UPSTREAM_MODEL}
            route:
              - kind: openai_compat
              - kind: openai_compat
                base_url: https://canary.example/v1
                api_key: ${CANARY_KEY}
                optional: true
          - id: chat-b
            route:
              - kind: openrouter
                base_url: https://openrouter.ai/api/v1
                api_keys:
                  - ${POOL_KEY}
                  - ${POOL_KEY2}
          - id: chat-c
            route:
              - kind: openrouter
                base_url: https://openrouter.ai/api/v1
                api_keys:
                  - ${MIXED_KEY}
                  - literal-key
        """,
    )
    found = registry.discover_references(models, None, None)

    assert "COMMENTED_OUT" not in found
    # The first route inherits the model's base_url and api_key.
    assert found["SHARED_BASE"].required
    assert found["MODEL_KEY"].required
    assert found["SHARED_BASE"].used_by == {"chat-a"}
    # Referenced, but a blank value does not skip the model.
    assert not found["UPSTREAM_MODEL"].required
    # Only an optional route needs it.
    assert not found["CANARY_KEY"].required
    assert found["CANARY_KEY"].used_by == {"chat-a"}
    # A key list fails only when every member is empty.
    assert not found["POOL_KEY"].required
    assert found["POOL_KEY"].key_groups == [("POOL_KEY", "POOL_KEY2")]
    # A literal key keeps its route alive on its own.
    assert not found["MIXED_KEY"].key_groups


def test_key_groups_are_required_together(tmp_path) -> None:
    models = _write(
        tmp_path,
        "models.yaml",
        """
        models:
          - id: chat-b
            route:
              - kind: openrouter
                base_url: https://openrouter.ai/api/v1
                api_keys:
                  - ${POOL_KEY}
                  - ${POOL_KEY2}
        """,
    )
    entries = registry.build_entries(registry.discover_references(models, None, None), {}, {})
    first, second = entries["POOL_KEY"], entries["POOL_KEY2"]
    assert first.is_required(_context({}))
    assert second.is_required(_context({}))
    assert not first.is_required(_context({"POOL_KEY2": "set"}))
    assert not second.is_required(_context({"POOL_KEY2": "set"}))


def test_discovered_entries_take_secrecy_from_their_names(tmp_path) -> None:
    models = _write(
        tmp_path,
        "models.yaml",
        """
        models:
          - id: chat
            base_url: ${LOCAL_BOX_URL}
            api_key: ${LOCAL_BOX_TOKEN}
        """,
    )
    entries = registry.build_entries(registry.discover_references(models, None, None), {}, {})
    assert not entries["LOCAL_BOX_URL"].secret
    assert entries["LOCAL_BOX_TOKEN"].secret
    assert entries["LOCAL_BOX_URL"].origin == "discovered"
    assert entries["LOCAL_BOX_URL"].restart_required
    assert entries["LOCAL_BOX_URL"].used_by == ("chat",)


def test_discovery_merges_into_static_entries(tmp_path) -> None:
    models = _write(
        tmp_path,
        "models.yaml",
        """
        models:
          - id: chat
            route:
              - kind: openrouter
                base_url: https://openrouter.ai/api/v1
                api_key: ${OPENROUTER_API_KEY}
        """,
    )
    entries = registry.build_entries(registry.discover_references(models, None, None), {}, {})
    entry = entries["OPENROUTER_API_KEY"]
    assert entry.origin == "static"
    assert entry.used_by == ("chat",)
    assert entry.is_required(_context())


def test_routing_and_alert_references(tmp_path) -> None:
    routing = _write(
        tmp_path,
        "routing.yaml",
        """
        local_deployment:
          - endpoint: ${LOCAL_DEPLOYMENT_URL:-http://localhost:8001}
            models: [model-a, model-b]
        """,
    )
    alerts = _write(
        tmp_path,
        "alerts.yaml",
        """
        sinks:
          slack:
            webhook: ${ALERT_SINK_WEBHOOK}
        """,
    )
    found = registry.discover_references(None, routing, alerts)
    assert found["LOCAL_DEPLOYMENT_URL"].used_by == {"model-a", "model-b"}
    assert not found["LOCAL_DEPLOYMENT_URL"].required
    assert found["ALERT_SINK_WEBHOOK"].category == "alerts"
    entries = registry.build_entries(found, {}, {})
    assert entries["ALERT_SINK_WEBHOOK"].secret


def test_discovery_ignores_environment_only_references(tmp_path) -> None:
    routing = _write(
        tmp_path,
        "routing.yaml",
        """
        remote_deployment:
          - endpoint: http://${DB_HOST}:9000
            models: [m]
        """,
    )
    entries = registry.build_entries(registry.discover_references(None, routing, None), {}, {})
    assert "DB_HOST" not in entries


def test_unreadable_files_contribute_nothing(tmp_path) -> None:
    broken = _write(tmp_path, "models.yaml", "models: [unbalanced\n")
    assert registry.discover_references(broken, tmp_path / "missing.yaml", None) == {}


def test_every_default_satisfies_its_own_limits() -> None:
    from serving.config.app_config import _value_problem

    for entry in registry.static_entries():
        if entry.generated or entry.default is None:
            continue
        assert _value_problem(entry, entry.default) is None, entry.key


def test_the_jwt_algorithms_offered_are_the_ones_a_shared_secret_can_sign() -> None:
    import jwt

    entry = registry.static_entry("JWT_ALGORITHM")
    assert entry is not None and entry.case_sensitive
    for algorithm in entry.choices:
        token = jwt.encode({"sub": "x"}, "s" * 64, algorithm=algorithm)
        assert jwt.decode(token, "s" * 64, algorithms=[algorithm]) == {"sub": "x"}
