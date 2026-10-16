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
        args = typing.get_args(annotation)
        if type(None) in args:
            non_null = [arg for arg in args if arg is not type(None)]
            assert len(non_null) == 1, entry.key
            annotation = non_null[0]
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
    "email_provider": "tencent_ses",
    "tencent_ses_region": "ap-hongkong",
    "cors_allowed_origins": "https://x.example",
    "trusted_proxies": "10.0.0.0/8",
    "trusted_direct_client_networks": "192.168.0.0/16",
    "trusted_cloudflare_networks": "10.1.0.0/16",
    "provider_route_types": "chutes=quota",
    "cookie_samesite": "strict",
    "cookie_domain": "example.com",
    "sms_provider": "tencent",
    "sms_default_country_code": "86",
    "sms_allowed_prefixes": "+86",
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


# --- secrecy -----------------------------------------------------------------


def test_the_secret_name_words() -> None:
    """The console's add-variable form mirrors this list."""
    assert registry.SECRET_NAME_WORDS == (
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASS",
        "PWD",
        "WEBHOOK",
        "PRIVATE",
        "AUTH",
        "CREDENTIAL",
        "BEARER",
        "CERT",
        "DSN",
        "SA_JSON",
    )


@pytest.mark.parametrize(
    "name",
    [
        "MY_PROVIDER_API_KEY",
        "ACME_CREDENTIALS",
        "AZURE_OPENAI_CREDENTIAL",
        "GLM_PASS",
        "DB_PWD",
        "GLM_AUTH",
        "SENTRY_DSN",
        "UPSTREAM_BEARER",
        "CLIENT_CERT",
        "VERTEX_SA_JSON",
        "SLACK_WEBHOOK",
        "lower_case_token",
        # Field names, too.
        "api_key",
        "api_keys",
        "Authorization",
    ],
)
def test_names_that_look_like_a_credential(name: str) -> None:
    assert registry.is_secret_name(name)


@pytest.mark.parametrize("name", ["ACME_REGION", "LOCAL_BOX_URL", "base_url", "embeddings_path"])
def test_names_that_do_not(name: str) -> None:
    assert not registry.is_secret_name(name)


def test_a_custom_entry_follows_its_name_unless_flagged() -> None:
    assert registry.custom_entry("ACME_PASS").secret
    assert registry.custom_entry("ACME_PASS", secret=False).secret
    assert not registry.custom_entry("ACME_REGION").secret
    assert registry.custom_entry("ACME_REGION", secret=True).secret


def test_discovery_marks_references_from_credential_fields(tmp_path) -> None:
    models = _write(
        tmp_path,
        "models.yaml",
        """
        models:
          - id: chat-a
            base_url: ${ACME_BASE}
            api_key: ${ACME_ONE}
            provider_model_id: ${ACME_MODEL}
          - id: chat-b
            route:
              - kind: openrouter
                base_url: https://openrouter.ai/api/v1
                api_keys:
                  - ${ACME_TWO}
                  - ${ACME_THREE}
              - kind: openai_compat
                base_url: https://canary.example/v1
                api_key: ${ACME_FOUR}
                optional: true
              - kind: openai_compat
                base_url: https://other.example/v1
                headers:
                  Authorization: Bearer ${ACME_FIVE}
        """,
    )
    routing = _write(
        tmp_path,
        "routing.yaml",
        """
        remote_deployment:
          - endpoint: ${ACME_ENDPOINT}
            token: ${ACME_SIX}
            models: [chat-a]
        """,
    )
    found = registry.discover_references(models, routing, None)

    for key in ("ACME_ONE", "ACME_TWO", "ACME_THREE", "ACME_FOUR", "ACME_FIVE", "ACME_SIX"):
        assert found[key].credential, key
    for key in ("ACME_BASE", "ACME_MODEL", "ACME_ENDPOINT"):
        assert not found[key].credential, key
    entries = registry.build_entries(found, {}, {})
    assert entries["ACME_ONE"].secret and entries["ACME_FIVE"].secret
    assert not entries["ACME_BASE"].secret


def test_a_static_entry_referenced_as_a_credential_is_secret(tmp_path) -> None:
    models = _write(
        tmp_path,
        "models.yaml",
        """
        models:
          - id: chat
            base_url: ${SITE_PUBLIC_BASE_URL}
            api_key: ${SITE_NAME}
        """,
    )
    entries = registry.build_entries(registry.discover_references(models, None, None), {}, {})

    assert entries["SITE_NAME"].secret
    assert entries["SITE_NAME"].origin == "static"
    assert not entries["SITE_PUBLIC_BASE_URL"].secret


def test_a_rows_secret_flag_is_kept_by_every_entry(tmp_path) -> None:
    models = _write(
        tmp_path,
        "models.yaml",
        """
        models:
          - id: chat
            base_url: ${LOCAL_BOX_URL}
            api_key: literal-key
        """,
    )
    found = registry.discover_references(models, None, None)

    entries = registry.build_entries(
        found,
        {"SITE_NAME": True, "MY_BOX_REGION": True, "MY_OTHER_REGION": False},
        {},
        {"LOCAL_BOX_URL": True, "FORGOTTEN_URL": True, "SITE_DOCS_URL": False},
    )

    assert entries["SITE_NAME"].secret  # static, flagged on its row
    assert entries["MY_BOX_REGION"].secret  # custom, flagged
    assert not entries["MY_OTHER_REGION"].secret
    assert entries["LOCAL_BOX_URL"].secret  # discovered, flagged on its reset row
    assert not entries["SITE_DOCS_URL"].secret
    # A reset row lists no entry of its own.
    assert "FORGOTTEN_URL" not in entries


# --- phone authentication ----------------------------------------------------

#: What serving.utils.tencent_sms.is_configured() needs, by Settings field.
_SMS_NEEDS = (
    "tencent_cloud_secret_id",
    "tencent_cloud_secret_key",
    "sms_sdk_app_id",
    "sms_sign_name",
    "sms_template_id",
    "sms_region",
)


def test_the_sms_requirements_are_what_the_adapter_needs(monkeypatch) -> None:
    from serving.utils import tencent_sms

    complete = dict.fromkeys(_SMS_NEEDS, "set")
    monkeypatch.setattr(tencent_sms, "settings", Settings(**complete))
    assert tencent_sms.is_configured()
    context = _context({"SMS_AUTH_ENABLED": "true", "SMS_PROVIDER": "tencent"})
    required = {
        entry.field
        for entry in registry.static_entries()
        if entry.category in ("phone", "integrations") and entry.is_required(context)
    }
    for name in _SMS_NEEDS:
        monkeypatch.setattr(tencent_sms, "settings", Settings(**{**complete, name: ""}))
        assert not tencent_sms.is_configured(), name
    assert required == set(_SMS_NEEDS)


@pytest.mark.parametrize(
    "values,required",
    [
        ({"SMS_AUTH_ENABLED": "true", "SMS_PROVIDER": "tencent"}, True),
        ({"SMS_AUTH_ENABLED": "1", "SMS_PROVIDER": "tencent"}, True),
        ({"SMS_AUTH_ENABLED": "false", "SMS_PROVIDER": "tencent"}, False),
        ({"SMS_AUTH_ENABLED": "", "SMS_PROVIDER": "tencent"}, False),
        ({"SMS_AUTH_ENABLED": "true", "SMS_PROVIDER": "none"}, False),
    ],
)
def test_sms_values_are_required_only_while_phone_sign_in_sends_through_tencent(
    values, required
) -> None:
    context = _context(values)
    for name in _SMS_NEEDS:
        entry = registry.static_entry(registry.settings_env_name(name))
        assert entry is not None
        assert entry.is_required(context) is required, name
    token = registry.static_entry("TENCENT_CLOUD_TOKEN")
    assert token is not None and not token.is_required(context)


def test_tencent_ses_still_needs_the_credentials_with_sms_off() -> None:
    context = _context({"SMS_AUTH_ENABLED": "false", "EMAIL_PROVIDER": "tencent_ses"})
    for key in ("TENCENT_CLOUD_SECRET_ID", "TENCENT_CLOUD_SECRET_KEY"):
        entry = registry.static_entry(key)
        assert entry is not None and entry.is_required(context)
    entry = registry.static_entry("SMS_SDK_APP_ID")
    assert entry is not None and not entry.is_required(context)
