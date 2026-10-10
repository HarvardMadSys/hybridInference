"""Database-backed configuration: resolution, overlay, boot import, health, writes."""

from __future__ import annotations

import logging
import textwrap

import pytest

from serving.config import app_config
from serving.config.app_config_registry import static_entries
from serving.config.settings import get_settings
from tests.fixtures.app_config_store import FakeAppConfigStore, row


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """A clean environment: no registered setting set, and config files under tmp_path."""
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    for key in ("ENVIRONMENT", "DISTRIBUTION_CONFIG_PATH"):
        monkeypatch.delenv(key, raising=False)
    models = tmp_path / "models.yaml"
    models.write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(models))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    get_settings.cache_clear()
    return tmp_path


def _write_models(path, text: str) -> None:
    (path / "models.yaml").write_text(textwrap.dedent(text))


async def _boot(store: FakeAppConfigStore) -> None:
    await app_config.load(store)
    app_config.attach(store)


# --- resolution --------------------------------------------------------------


def test_before_loading_the_resolver_is_the_environment(config_env, monkeypatch) -> None:
    monkeypatch.setenv("SITE_NAME", "From env")
    assert app_config.config_value("SITE_NAME") == "From env"
    assert app_config.config_value("SITE_DOCS_URL") is None
    assert app_config.config_value("SITE_DOCS_URL", "fallback") == "fallback"


@pytest.mark.asyncio
async def test_a_row_wins_over_the_environment_even_when_empty(config_env, monkeypatch) -> None:
    monkeypatch.setenv("SITE_NAME", "From env")
    monkeypatch.setenv("SITE_SUPPORT_EMAIL", "env@example.com")
    store = FakeAppConfigStore([row("SITE_NAME", "From db"), row("SITE_SUPPORT_EMAIL", "")])

    await _boot(store)

    assert app_config.config_value("SITE_NAME") == "From db"
    assert app_config.config_value("SITE_SUPPORT_EMAIL", "default") == ""
    assert app_config.config_value("SITE_DOCS_URL", "default") == "default"


@pytest.mark.asyncio
async def test_a_deleted_row_falls_back_to_the_environment(config_env, monkeypatch) -> None:
    monkeypatch.setenv("SITE_NAME", "From env")
    store = FakeAppConfigStore([row("SITE_NAME", "From db")])
    await _boot(store)

    await app_config.delete("SITE_NAME", updated_by="admin")

    assert app_config.config_value("SITE_NAME") == "From env"


# --- boot: import and generation ---------------------------------------------


@pytest.mark.asyncio
async def test_boot_imports_environment_values_without_overwriting_rows(
    config_env, monkeypatch
) -> None:
    monkeypatch.setenv("SITE_NAME", "Imported")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv("SITE_DOCS_URL", "   ")  # blank: not imported
    monkeypatch.setenv("FRONTEND_URL", "https://env.example")
    store = FakeAppConfigStore([row("FRONTEND_URL", "https://db.example")])

    await _boot(store)

    assert store.rows["SITE_NAME"].value == "Imported"
    assert store.rows["SITE_NAME"].source == "env_import"
    assert store.rows["SITE_NAME"].updated_by == "env-import"
    assert store.rows["SMTP_PASSWORD"].secret
    assert "SITE_DOCS_URL" not in store.rows
    assert store.rows["FRONTEND_URL"].value == "https://db.example"
    assert store.schema_calls == 1


@pytest.mark.asyncio
async def test_import_is_idempotent(config_env, monkeypatch) -> None:
    monkeypatch.setenv("SITE_NAME", "First")
    store = FakeAppConfigStore()
    await _boot(store)
    imported = dict(store.rows)

    monkeypatch.setenv("SITE_NAME", "Second")
    app_config.reset_state()
    await _boot(store)

    assert store.rows["SITE_NAME"] == imported["SITE_NAME"]
    assert app_config.config_value("SITE_NAME") == "First"


@pytest.mark.asyncio
async def test_boot_imports_numbered_and_discovered_variables(config_env, monkeypatch) -> None:
    _write_models(
        config_env,
        """
        models:
          - id: chat
            base_url: ${MY_BOX_URL}
        """,
    )
    monkeypatch.setenv("MY_BOX_URL", "http://box.internal:8000/v1")
    monkeypatch.setenv("MINIMAX_API_KEY2", "second-key")
    monkeypatch.setenv("UNRELATED_VARIABLE", "ignored")
    store = FakeAppConfigStore()

    await _boot(store)

    assert store.rows["MY_BOX_URL"].value == "http://box.internal:8000/v1"
    assert store.rows["MINIMAX_API_KEY2"].secret
    assert "UNRELATED_VARIABLE" not in store.rows


@pytest.mark.asyncio
async def test_boot_generates_missing_secrets(config_env) -> None:
    store = FakeAppConfigStore()

    await _boot(store)

    for key in ("JWT_SECRET_KEY", "API_KEY_SECRET"):
        stored = store.rows[key]
        assert stored.source == "generated"
        assert stored.secret
        assert len(stored.value) >= 64
    assert store.rows["JWT_SECRET_KEY"].value != store.rows["API_KEY_SECRET"].value
    assert get_settings().jwt_secret_key == store.rows["JWT_SECRET_KEY"].value
    assert get_settings().api_key_secret == store.rows["API_KEY_SECRET"].value
    # Nothing else is generated: the fence secret falls back to API_KEY_SECRET.
    assert "ERASURE_FENCE_SECRET" not in store.rows


@pytest.mark.asyncio
async def test_boot_keeps_secrets_from_the_environment(config_env, monkeypatch) -> None:
    monkeypatch.setenv("API_KEY_SECRET", "from-the-environment")
    store = FakeAppConfigStore(api_keys_exist=True)

    await _boot(store)

    assert store.rows["API_KEY_SECRET"].value == "from-the-environment"
    assert store.rows["API_KEY_SECRET"].source == "env_import"


@pytest.mark.asyncio
async def test_api_key_secret_is_not_generated_over_existing_api_keys(config_env) -> None:
    store = FakeAppConfigStore(api_keys_exist=True)

    with pytest.raises(app_config.ConfigBootstrapError, match="API_KEY_SECRET"):
        await app_config.load(store)

    # Nothing half-done: not even the JWT secret was written.
    assert "JWT_SECRET_KEY" not in store.rows


# --- overlay -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlay_updates_the_live_settings_object_in_place(config_env) -> None:
    live = get_settings()
    store = FakeAppConfigStore(
        [
            row("SMTP_PORT", "2525"),
            row("ALERTS_ENABLED", "true"),  # declared with alias=
            row("TRUST_PROXY_HEADERS", "1"),  # declared with AliasChoices
            row("TRUSTED_PROXIES", "10.0.0.0/8, 192.168.1.1/32"),
            row("CORS_ALLOWED_ORIGINS", "https://console.example, https://other.example"),
            row("BASE_URL", "https://gateway.example"),
        ]
    )

    await _boot(store)

    assert get_settings() is live
    assert live.smtp_port == 2525
    assert live.alerts_enabled is True
    assert live.trust_proxy_headers is True
    # Derived by a model validator, so it must be recomputed, not just copied.
    assert [str(network) for network in live.trusted_proxies_parsed] == [
        "10.0.0.0/8",
        "192.168.1.1/32",
    ]
    assert live.cors_allowed_origins == ["https://console.example", "https://other.example"]
    assert live.base_url == "https://gateway.example"
    assert "base_url" in live.model_fields_set


@pytest.mark.asyncio
async def test_an_invalid_stored_value_is_skipped_and_reported(config_env, monkeypatch) -> None:
    monkeypatch.setenv("SMTP_PORT", "465")
    get_settings.cache_clear()
    store = FakeAppConfigStore(
        [
            row("SMTP_PORT", "not-a-port"),
            row("SMTP_HOST", "mail.example"),
            row("TRUSTED_PROXIES", "bogus"),
        ]
    )

    await _boot(store)

    settings = get_settings()
    assert settings.smtp_port == 465  # the environment's value, not the bad row
    assert settings.smtp_host == "mail.example"  # the rest still applies
    assert settings.trusted_proxies == ""
    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert "integer" in entries["SMTP_PORT"]["invalid"]
    assert "CIDR" in entries["TRUSTED_PROXIES"]["invalid"]
    assert entries["SMTP_HOST"]["invalid"] is None


@pytest.mark.asyncio
async def test_a_cross_field_rule_rejects_only_the_value_that_breaks_it(config_env) -> None:
    store = FakeAppConfigStore(
        [
            row("TRUST_CLOUDFLARE_HEADERS", "true"),  # requires TRUST_PROXY_HEADERS
            row("TRUSTED_CLOUDFLARE_NETWORKS", "10.1.0.0/16"),
            row("SITE_NAME", "Still applied"),
        ]
    )

    await _boot(store)

    settings = get_settings()
    assert settings.trust_cloudflare_headers is False
    assert [str(network) for network in settings.trusted_cloudflare_parsed] == ["10.1.0.0/16"]
    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert "requires TRUST_PROXY_HEADERS" in entries["TRUST_CLOUDFLARE_HEADERS"]["invalid"]
    assert entries["TRUSTED_CLOUDFLARE_NETWORKS"]["invalid"] is None


@pytest.mark.asyncio
async def test_values_that_satisfy_a_rule_together_all_apply(config_env) -> None:
    store = FakeAppConfigStore(
        [
            row("TRUST_CLOUDFLARE_HEADERS", "true"),
            row("TRUSTED_CLOUDFLARE_NETWORKS", "10.1.0.0/16"),
            row("TRUST_PROXY_HEADERS", "true"),
        ]
    )

    await _boot(store)

    assert get_settings().trust_cloudflare_headers is True
    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    for key in ("TRUST_CLOUDFLARE_HEADERS", "TRUSTED_CLOUDFLARE_NETWORKS", "TRUST_PROXY_HEADERS"):
        assert entries[key]["invalid"] is None


# --- restart-required and live changes ---------------------------------------


@pytest.mark.asyncio
async def test_restart_required_values_keep_their_boot_value(config_env) -> None:
    store = FakeAppConfigStore(
        [row("ENABLE_ROUTEWISE", "false"), row("CIRCUIT_FAILURE_THRESHOLD", "3")]
    )
    await _boot(store)
    assert app_config.get_config_health().pending_restart == ()

    store.rows["ENABLE_ROUTEWISE"] = row("ENABLE_ROUTEWISE", "true")
    store.rows["CIRCUIT_FAILURE_THRESHOLD"] = row("CIRCUIT_FAILURE_THRESHOLD", "9")
    await app_config.refresh()

    assert get_settings().enable_routewise is False
    assert app_config.config_value("CIRCUIT_FAILURE_THRESHOLD") == "3"
    assert set(app_config.get_config_health().pending_restart) == {
        "ENABLE_ROUTEWISE",
        "CIRCUIT_FAILURE_THRESHOLD",
    }


@pytest.mark.asyncio
async def test_an_equivalent_spelling_is_not_pending(config_env, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ROUTEWISE", "1")
    get_settings.cache_clear()
    store = FakeAppConfigStore()
    await _boot(store)  # imports "1"

    store.rows["ENABLE_ROUTEWISE"] = row("ENABLE_ROUTEWISE", "true")
    await app_config.refresh()

    assert "ENABLE_ROUTEWISE" not in app_config.get_config_health().pending_restart


@pytest.mark.asyncio
async def test_live_values_apply_on_refresh(config_env) -> None:
    store = FakeAppConfigStore([row("BASE_URL", "https://old.example")])
    await _boot(store)

    store.rows["BASE_URL"] = row("BASE_URL", "https://new.example")
    await app_config.refresh()

    assert get_settings().base_url == "https://new.example"
    assert app_config.config_value("BASE_URL") == "https://new.example"
    assert app_config.get_config_health().pending_restart == ()


@pytest.mark.asyncio
async def test_a_stale_refresh_never_replaces_a_newer_apply(config_env) -> None:
    store = FakeAppConfigStore([row("SITE_NAME", "old")])
    await _boot(store)

    class _SlowStore(FakeAppConfigStore):
        intercepted = False

        async def fetch_all(self):
            snapshot = await super().fetch_all()
            if not self.intercepted:
                self.intercepted = True
                # An administrator's write lands while this read is in flight.
                await app_config.update({"SITE_NAME": "new"}, updated_by="admin")
            return snapshot

    slow = _SlowStore(store.rows.values())
    app_config.attach(slow)
    await app_config.refresh()

    assert app_config.config_value("SITE_NAME") == "new"


def test_listeners_recompute_module_constants(config_env, monkeypatch) -> None:
    import routing.prefill_load as prefill_load
    import routing.routers as routers
    from serving.adapters import openai_compat
    from serving.servers.routers import anthropic_messages

    app_config._apply(
        {
            "ROUTING_AFFINITY_ENABLED": row("ROUTING_AFFINITY_ENABLED", "0"),
            "ROUTING_AFFINITY_MAX_AGE_SEC": row("ROUTING_AFFINITY_MAX_AGE_SEC", "60"),
            "ROUTING_PREFILL_AWARE_ENABLED": row("ROUTING_PREFILL_AWARE_ENABLED", "0"),
            "ROUTING_PREFILL_ELEPHANT_TOKENS": row("ROUTING_PREFILL_ELEPHANT_TOKENS", "1000"),
            "ROUTING_PRIORITY_INTERACTIVE": row("ROUTING_PRIORITY_INTERACTIVE", "30"),
            "UPSTREAM_COMPLETION_TIMEOUT_S": row("UPSTREAM_COMPLETION_TIMEOUT_S", "42"),
            "STREAM_MAX_IDLE_S": row("STREAM_MAX_IDLE_S", "100"),
            "STREAM_MAX_FIRST_FRAME_IDLE_S": row("STREAM_MAX_FIRST_FRAME_IDLE_S", "50"),
            "SMALL_MAXTOK_REASONING_THRESHOLD": row("SMALL_MAXTOK_REASONING_THRESHOLD", "0"),
        },
        boot=True,
    )

    assert routers.AFFINITY_ENABLED is False
    assert routers.AFFINITY_MAX_AGE_SECONDS == 60.0
    assert prefill_load.PREFILL_AWARE_ENABLED is False
    assert prefill_load.ELEPHANT_TOKENS == 1000
    assert prefill_load.PRIORITY_INTERACTIVE == 30
    # A tracker built before the change follows it too.
    assert prefill_load.PrefillLoadTracker().is_elephant(1000)
    assert openai_compat._COMPLETION_TIMEOUT_S == 42.0
    assert anthropic_messages._MAX_STREAM_IDLE == 100
    # Never less head room for the first frame than for the stream.
    assert anthropic_messages._MAX_FIRST_FRAME_IDLE == 100
    assert anthropic_messages._SMALL_MAXTOK_THRESHOLD == 0

    app_config.reset_state()

    assert routers.AFFINITY_ENABLED is True
    assert prefill_load.ELEPHANT_TOKENS == 200_000
    assert openai_compat._COMPLETION_TIMEOUT_S == 600.0
    assert anthropic_messages._MAX_STREAM_IDLE == 240


def test_log_level_and_format_follow_the_stored_value(config_env, monkeypatch) -> None:
    from serving.utils.logging import JsonFormatter

    monkeypatch.setenv("LOG_LEVEL", "INFO")
    root = logging.getLogger()
    level, formatters = root.level, [handler.formatter for handler in root.handlers]
    try:
        app_config._apply(
            {"LOG_LEVEL": row("LOG_LEVEL", "WARNING"), "LOG_FORMAT": row("LOG_FORMAT", "json")},
            boot=True,
        )
        assert root.level == logging.WARNING
        assert all(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers)
    finally:
        app_config.reset_state()
        root.setLevel(level)
        for handler, formatter in zip(root.handlers, formatters, strict=False):
            handler.setFormatter(formatter)


# --- health ------------------------------------------------------------------


def test_missing_secrets_are_reported_when_the_database_was_unreachable(config_env) -> None:
    app_config.use_environment(database_enabled=True)

    missing = app_config.get_config_health().missing
    assert "JWT_SECRET_KEY" in missing
    assert "API_KEY_SECRET" in missing
    assert app_config.get_config_health().incomplete


def test_a_database_free_router_without_auth_needs_no_secrets(config_env, monkeypatch) -> None:
    monkeypatch.setenv("USER_AUTH_ENABLED", "false")
    monkeypatch.setenv("SIGNUP_ENABLED", "false")
    get_settings.cache_clear()

    app_config.use_environment(database_enabled=False)

    assert app_config.get_config_health().missing == ()


@pytest.mark.parametrize(
    "signup_enabled,verification,required",
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_smtp_is_required_only_while_signup_needs_verification(
    config_env, monkeypatch, signup_enabled, verification, required
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from serving.config.runtime_settings import init_runtime_settings

    runtime = init_runtime_settings(MagicMock(list_settings=AsyncMock(return_value=[])))
    runtime.set_cached("signup_enabled", signup_enabled)
    runtime.set_cached("signup_require_email_verification", verification)

    app_config.use_environment(database_enabled=True)

    missing = app_config.get_config_health().missing
    assert ("SMTP_PASSWORD" in missing) is required
    # SMTP_USER defaults to "resend", so it is set either way.
    assert "SMTP_USER" not in missing


def test_a_variable_a_route_needs_is_missing_until_set(config_env, monkeypatch) -> None:
    _write_models(
        config_env,
        """
        models:
          - id: chat
            base_url: https://api.example/v1
            api_key: ${CHAT_UPSTREAM_KEY}
          - id: canary
            route:
              - kind: openai_compat
                base_url: https://canary.example/v1
                api_key: ${CANARY_KEY}
                optional: true
        """,
    )
    app_config.use_environment(database_enabled=False)
    health = app_config.get_config_health()
    assert "CHAT_UPSTREAM_KEY" in health.missing
    assert "CANARY_KEY" not in health.missing

    monkeypatch.setenv("CHAT_UPSTREAM_KEY", "now-set")
    assert "CHAT_UPSTREAM_KEY" not in app_config.refresh_health().missing


# --- describe ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_never_returns_a_secret(config_env, monkeypatch) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "env-password")
    store = FakeAppConfigStore([row("SMTP_PASSWORD", "db-password", secret=True)])
    await _boot(store)

    body = app_config.describe()
    entries = {entry["key"]: entry for entry in body["entries"]}
    smtp = entries["SMTP_PASSWORD"]
    assert smtp["secret"] is True
    assert smtp["value"] is None and smtp["default"] is None
    assert smtp["is_set"] is True
    assert smtp["source"] == "database"
    assert smtp["environment_ignored"] is True
    for generated in ("JWT_SECRET_KEY", "API_KEY_SECRET"):
        assert entries[generated]["value"] is None
    text = repr(body)
    assert "db-password" not in text and "env-password" not in text
    for secret in (store.rows["JWT_SECRET_KEY"].value, store.rows["API_KEY_SECRET"].value):
        assert secret not in text


@pytest.mark.asyncio
async def test_describe_types_values_and_names_their_source(config_env, monkeypatch) -> None:
    monkeypatch.setenv("SMTP_PORT", "465")
    store = FakeAppConfigStore(
        [row("COOKIE_SECURE", "false"), row("ROUTING_AFFINITY_ENABLED", "0")]
    )
    await _boot(store)

    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["COOKIE_SECURE"]["value"] is False
    assert entries["COOKIE_SECURE"]["default"] is True
    assert entries["ROUTING_AFFINITY_ENABLED"]["value"] is False
    assert entries["SMTP_PORT"]["value"] == 465
    assert entries["SMTP_PORT"]["source"] == "database"  # imported at boot
    assert entries["SITE_DOCS_URL"]["source"] == "default"
    assert entries["CORS_ALLOWED_ORIGINS"]["value"].startswith("http://localhost:3000,")
    assert entries["SMTP_PASSWORD"]["setup"] is True


@pytest.mark.asyncio
async def test_log_format_text_is_a_valid_spelling_of_plain(config_env, monkeypatch) -> None:
    # The logger treats every value but "json" as plain; "text" is in use.
    monkeypatch.setenv("LOG_FORMAT", "text")
    store = FakeAppConfigStore()
    await _boot(store)

    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["LOG_FORMAT"]["value"] == "text"
    assert entries["LOG_FORMAT"]["invalid"] is None


@pytest.mark.asyncio
async def test_describe_compares_the_environment_as_a_typed_value(config_env, monkeypatch) -> None:
    # Compose injects "1"; a value saved from the console is stored as "true".
    monkeypatch.setenv("COOKIE_SECURE", "1")
    monkeypatch.setenv("SMTP_PORT", "587")
    store = FakeAppConfigStore([row("COOKIE_SECURE", "true"), row("SMTP_PORT", "465")])
    await _boot(store)

    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["COOKIE_SECURE"]["environment_ignored"] is False
    assert entries["SMTP_PORT"]["environment_ignored"] is True


# --- writes ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_stores_values_in_their_environment_form(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    changes = await app_config.update(
        {
            "COOKIE_SECURE": False,
            "ROUTING_AFFINITY_ENABLED": False,
            "SMTP_PORT": 2525,
            "CIRCUIT_COOLDOWN_SECONDS": 12.5,
            "CORS_ALLOWED_ORIGINS": "https://a.example,https://b.example",
            "SMTP_PASSWORD": "hunter2",
        },
        updated_by="admin@example.com",
    )

    assert store.rows["COOKIE_SECURE"].value == "false"
    assert store.rows["ROUTING_AFFINITY_ENABLED"].value == "0"
    assert store.rows["SMTP_PORT"].value == "2525"
    assert store.rows["CIRCUIT_COOLDOWN_SECONDS"].value == "12.5"
    assert store.rows["SMTP_PASSWORD"].secret
    assert store.rows["SMTP_PASSWORD"].updated_by == "admin@example.com"
    assert len(store.writes) == 1  # one transaction
    # Live entries apply at once in this process.
    assert get_settings().smtp_port == 2525
    assert get_settings().cors_allowed_origins == ["https://a.example", "https://b.example"]
    audit = {change.key: change.audit_details() for change in changes}
    assert audit["SMTP_PASSWORD"] == {"key": "SMTP_PASSWORD", "changed": True}
    assert audit["SMTP_PORT"] == {"key": "SMTP_PORT", "old_value": 587, "new_value": 2525}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values,detail",
    [
        ({"SMTP_PORT": "2525"}, "SMTP_PORT: must be an integer"),
        ({"SMTP_PORT": True}, "SMTP_PORT: must be an integer"),
        ({"SMTP_PORT": 25.5}, "SMTP_PORT: must be an integer"),
        ({"SMTP_PORT": 0}, "SMTP_PORT: must be at least 1"),
        ({"COOKIE_SECURE": "false"}, "COOKIE_SECURE: must be true or false"),
        ({"SITE_NAME": 5}, "SITE_NAME: must be a string"),
        ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL: must be one of"),
        ({"JWT_SECRET_KEY": "  "}, "JWT_SECRET_KEY: must not be blank"),
        ({"SITE_NAME": None}, "SITE_NAME: a value is required"),
        ({"TRUSTED_PROXIES": "not-a-cidr"}, "TRUSTED_PROXIES:"),
        ({"PROVIDER_ROUTE_TYPES": "chutes=forever"}, "PROVIDER_ROUTE_TYPES:"),
        ({"lowercase_name": "x"}, "lowercase_name: a name is"),
        ({"USER_AUTH_ENABLED": "true"}, "USER_AUTH_ENABLED: change it on the Settings tab"),
    ],
)
async def test_update_rejects_invalid_values(config_env, values, detail) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.update(values, updated_by="admin")

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail.startswith(detail)
    assert store.writes == []


@pytest.mark.asyncio
async def test_a_batch_is_stored_whole_or_not_at_all(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    with pytest.raises(app_config.ConfigUpdateError):
        await app_config.update({"SITE_NAME": "fine", "SMTP_PORT": "bad"}, updated_by="admin")

    assert store.writes == []
    assert "SITE_NAME" not in store.rows


@pytest.mark.asyncio
async def test_a_batch_may_satisfy_a_cross_field_rule_together(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.update({"TRUST_X_REAL_IP": True}, updated_by="admin")
    assert excinfo.value.detail == "TRUST_X_REAL_IP: TRUST_X_REAL_IP requires TRUST_PROXY_HEADERS"

    await app_config.update(
        {"TRUST_X_REAL_IP": True, "TRUST_PROXY_HEADERS": True}, updated_by="admin"
    )
    assert get_settings().trust_x_real_ip is True


@pytest.mark.asyncio
async def test_environment_only_names_are_forbidden(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    for call in (
        app_config.update({"DB_PASSWORD": "x"}, updated_by="admin"),
        app_config.delete("DB_HOST", updated_by="admin"),
    ):
        with pytest.raises(app_config.ConfigUpdateError) as excinfo:
            await call
        assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_immutable_secrets_cannot_change_once_set(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)  # generates API_KEY_SECRET

    for call in (
        app_config.update({"API_KEY_SECRET": "replacement"}, updated_by="admin"),
        # The fence secret falls back to API_KEY_SECRET, which is already in use.
        app_config.update({"ERASURE_FENCE_SECRET": "dedicated"}, updated_by="admin"),
        app_config.delete("API_KEY_SECRET", updated_by="admin"),
    ):
        with pytest.raises(app_config.ConfigUpdateError) as excinfo:
            await call
        assert excinfo.value.status_code == 409
    assert store.writes == []


@pytest.mark.asyncio
async def test_custom_variables_take_their_secret_flag_when_added(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    await app_config.update(
        {"MY_PRIVATE_BOX_TOKEN": "t0k3n", "MY_BOX_REGION": "east"},
        {"MY_PRIVATE_BOX_TOKEN": True},
        updated_by="admin",
    )
    assert store.rows["MY_PRIVATE_BOX_TOKEN"].secret
    assert not store.rows["MY_BOX_REGION"].secret

    # Changing an existing custom variable keeps its flag.
    await app_config.update(
        {"MY_PRIVATE_BOX_TOKEN": "rotated"}, {"MY_PRIVATE_BOX_TOKEN": False}, updated_by="admin"
    )
    assert store.rows["MY_PRIVATE_BOX_TOKEN"].secret

    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["MY_PRIVATE_BOX_TOKEN"]["custom"] is True
    assert entries["MY_PRIVATE_BOX_TOKEN"]["value"] is None
    # Captured by the model registry at startup.
    assert entries["MY_BOX_REGION"]["pending_restart"] is True

    await app_config.delete("MY_BOX_REGION", updated_by="admin")
    assert "MY_BOX_REGION" not in {entry["key"] for entry in app_config.describe()["entries"]}


@pytest.mark.asyncio
async def test_delete_refusals(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.delete("SITE_NAME", updated_by="admin")
    assert excinfo.value.status_code == 404

    # A generated secret with nothing in the environment to fall back to.
    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.delete("JWT_SECRET_KEY", updated_by="admin")
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_writes_need_the_database(config_env) -> None:
    app_config.use_environment(database_enabled=False)

    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.update({"SITE_NAME": "x"}, updated_by="admin")
    assert excinfo.value.status_code == 503


# --- limits that keep one value from locking everyone out --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values,detail",
    [
        ({"JWT_ALGORITHM": "HS265"}, "JWT_ALGORITHM: must be one of HS256, HS384, HS512."),
        # PyJWT names algorithms exactly; "hs256" signs nothing.
        ({"JWT_ALGORITHM": "hs256"}, "JWT_ALGORITHM: must be one of HS256, HS384, HS512."),
        ({"JWT_ALGORITHM": ""}, "JWT_ALGORITHM: must be one of HS256, HS384, HS512."),
        (
            {"JWT_ACCESS_TOKEN_EXPIRE_MINUTES": 43_201},
            "JWT_ACCESS_TOKEN_EXPIRE_MINUTES: must be at most 43200.",
        ),
        (
            {"JWT_REFRESH_TOKEN_EXPIRE_DAYS": 10**10},
            "JWT_REFRESH_TOKEN_EXPIRE_DAYS: must be at most 3650.",
        ),
        ({"STREAM_MAX_IDLE_S": 0}, "STREAM_MAX_IDLE_S: must be at least 1."),
        ({"REQUEST_TIMEOUT_SECONDS": 0.01}, "REQUEST_TIMEOUT_SECONDS: must be at least 1."),
        ({"COOKIE_SAMESITE": ""}, "COOKIE_SAMESITE: must be one of lax, strict, none."),
        ({"SMTP_PORT": 70_000}, "SMTP_PORT: must be at most 65535."),
    ],
)
async def test_values_that_would_break_sign_in_or_every_request_are_refused(
    config_env, values, detail
) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    with pytest.raises(app_config.ConfigUpdateError) as excinfo:
        await app_config.update(values, updated_by="admin")

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == detail
    assert store.writes == []


@pytest.mark.asyncio
async def test_values_within_the_limits_are_accepted(config_env) -> None:
    store = FakeAppConfigStore()
    await _boot(store)

    await app_config.update(
        {
            "JWT_ALGORITHM": "HS512",
            "JWT_ACCESS_TOKEN_EXPIRE_MINUTES": 43_200,
            "JWT_REFRESH_TOKEN_EXPIRE_DAYS": 3650,
            "COOKIE_SAMESITE": "Strict",
        },
        updated_by="admin",
    )

    settings = get_settings()
    assert settings.jwt_algorithm == "HS512"
    assert settings.jwt_access_token_expire_minutes == 43_200
    assert settings.cookie_samesite == "Strict"


@pytest.mark.asyncio
async def test_a_stored_value_outside_the_limits_is_reported_and_skipped(config_env) -> None:
    """A row the registry refuses never applies: the environment or default does."""
    import routing.prefill_load  # noqa: F401 — registers its listener
    from serving.servers.routers import anthropic_messages

    store = FakeAppConfigStore(
        [
            row("JWT_ALGORITHM", "HS265"),  # Settings alone would accept it
            row("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "10000000000"),
            row("STREAM_MAX_IDLE_S", "0"),  # read through config_value
            row("SITE_NAME", "Still applied"),
        ]
    )

    await _boot(store)

    settings = get_settings()
    assert settings.jwt_algorithm == "HS256"
    assert settings.jwt_refresh_token_expire_days == 30
    assert app_config.config_value("STREAM_MAX_IDLE_S") is None
    assert anthropic_messages._MAX_STREAM_IDLE == 240
    assert app_config.config_value("SITE_NAME") == "Still applied"
    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["JWT_ALGORITHM"]["invalid"] == "must be one of HS256, HS384, HS512"
    assert entries["JWT_REFRESH_TOKEN_EXPIRE_DAYS"]["invalid"] == "must be at most 3650"
    assert entries["STREAM_MAX_IDLE_S"]["invalid"] == "must be at least 1"


@pytest.mark.asyncio
async def test_an_environment_value_outside_the_limits_is_imported_but_skipped(
    config_env, monkeypatch
) -> None:
    monkeypatch.setenv("STREAM_MAX_IDLE_S", "0")
    store = FakeAppConfigStore()

    await _boot(store)

    assert store.rows["STREAM_MAX_IDLE_S"].value == "0"
    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["STREAM_MAX_IDLE_S"]["invalid"] == "must be at least 1"
    # Skipping the row leaves the environment in charge, as before the import.
    assert app_config.config_value("STREAM_MAX_IDLE_S") == "0"


# --- environment names Settings matches case-insensitively -------------------


@pytest.mark.asyncio
async def test_a_lower_case_secret_in_the_environment_is_imported(config_env, monkeypatch) -> None:
    """Upgrade path: `api_key_secret=` in .env has always configured the gateway."""
    monkeypatch.setenv("api_key_secret", "lower-case-api-secret")
    monkeypatch.setenv("jwt_secret_key", "lower-case-jwt-secret")
    store = FakeAppConfigStore(api_keys_exist=True)

    await _boot(store)

    assert store.rows["API_KEY_SECRET"].value == "lower-case-api-secret"
    assert store.rows["API_KEY_SECRET"].source == "env_import"
    assert store.rows["JWT_SECRET_KEY"].value == "lower-case-jwt-secret"
    assert get_settings().jwt_secret_key == "lower-case-jwt-secret"


@pytest.mark.asyncio
async def test_the_exact_upper_case_name_wins(config_env, monkeypatch) -> None:
    monkeypatch.setenv("site_name", "lower")
    monkeypatch.setenv("smtp_host", "lower.example")
    monkeypatch.setenv("SMTP_HOST", "upper.example")
    store = FakeAppConfigStore()

    await _boot(store)

    assert store.rows["SMTP_HOST"].value == "upper.example"
    # SITE_NAME is read through config_value with its exact name, never folded.
    assert "SITE_NAME" not in store.rows


def test_describe_finds_a_lower_case_environment_value(config_env, monkeypatch) -> None:
    monkeypatch.setenv("smtp_password", "lower-case-smtp")
    get_settings.cache_clear()

    app_config.use_environment(database_enabled=True)

    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["SMTP_PASSWORD"]["source"] == "environment"
    assert entries["SMTP_PASSWORD"]["is_set"] is True
    assert "SMTP_PASSWORD" not in app_config.get_config_health().missing


@pytest.mark.asyncio
async def test_a_lower_case_environment_value_is_compared_for_environment_ignored(
    config_env, monkeypatch
) -> None:
    monkeypatch.setenv("smtp_host", "env.example")
    store = FakeAppConfigStore([row("SMTP_HOST", "db.example")])
    await _boot(store)

    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["SMTP_HOST"]["environment_ignored"] is True
