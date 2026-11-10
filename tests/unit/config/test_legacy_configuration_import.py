"""The explicit legacy importer never becomes a boot fallback or overwrites settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serving.config import app_config
from serving.config.import_legacy import import_legacy
from serving.config.settings import get_settings
from tests.fixtures.app_config_store import FakeAppConfigStore, row

#: Where the previous release took its files and its manifest's mode from.
_PREVIOUS_RELEASE_SELECTORS = (
    "MODELS_CONFIG_PATH",
    "MODELS_CONFIG",
    "ROUTING_CONFIG_PATH",
    "ROUTING_CONFIG",
    "ALERTS_CONFIG_PATH",
    "DISTRIBUTION_CONFIG_PATH",
    "DISTRIBUTION_CONFIG_MODE",
)


@pytest.fixture(autouse=True)
def previous_release(monkeypatch):
    """An active manifest and no file named in the environment, unless a test says otherwise."""
    for key in _PREVIOUS_RELEASE_SELECTORS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    get_settings.cache_clear()


def set_environment(monkeypatch, **values):
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    get_settings.cache_clear()


async def configure(rows=()):
    store = FakeAppConfigStore(list(rows))
    await app_config.load(store, bootstrap=False)
    app_config.attach(store)
    return store


async def test_import_keeps_existing_catalog_and_does_not_require_its_retired_file(tmp_path):
    store = await configure([row("MODEL_CATALOG", "{}")])
    manifest = tmp_path / "legacy.yaml"
    manifest.write_text(
        "schema_version: 1\ndistribution: {id: imported}\npaths: {models: gone.yaml, alerts: alerts.yaml}\n"
    )
    (tmp_path / "alerts.yaml").write_text("rules: {}\n")
    saved = await import_legacy(manifest)
    assert set(saved) == {"SITE_CONFIGURATION", "ALERT_RULES"}
    assert store.rows["MODEL_CATALOG"].value == "{}"
    assert json.loads(store.rows["SITE_CONFIGURATION"].value)["distribution"]["id"] == "imported"


async def test_import_lifts_credentials_and_marks_them_secret(tmp_path):
    store = await configure()
    manifest = tmp_path / "legacy.yaml"
    manifest.write_text(
        "schema_version: 1\ndistribution: {id: imported}\npaths: {models: models.yaml}\n"
    )
    (tmp_path / "models.yaml").write_text(
        "models:\n- id: chat\n  name: Chat\n  provider: vllm\n  base_url: http://localhost:9/v1\n  api_key: old-provider-credential\n"
    )
    await import_legacy(manifest)
    catalog = store.rows["MODEL_CATALOG"].value
    assert "old-provider-credential" not in catalog
    name = json.loads(catalog)["models"][0]["api_key"][2:-1]
    assert store.rows[name].secret and store.rows[name].value == "old-provider-credential"


async def test_invalid_legacy_input_never_prints_its_secret_or_changes_rows(tmp_path):
    store = await configure()
    manifest = tmp_path / "legacy.yaml"
    manifest.write_text("private-credential: [unclosed\n")
    with pytest.raises(ValueError) as error:
        await import_legacy(manifest)
    assert "private-credential" not in str(error.value)
    assert store.rows == {}


async def test_public_branding_keeps_the_displayed_key_variable_name(tmp_path):
    store = await configure()
    root = Path(__file__).resolve().parents[3]
    branding = (root / "config/examples/branding.example.json").read_text()
    (tmp_path / "branding.json").write_text(branding)
    manifest = tmp_path / "legacy.yaml"
    manifest.write_text(
        "schema_version: 1\ndistribution: {id: imported}\nsite: {branding: branding.json}\n"
    )
    await import_legacy(manifest)
    assert json.loads(store.rows["SITE_BRANDING"].value)["example"]["api_key_env_var"] == (
        "EXAMPLE_INFERENCE_API_KEY"
    )
    assert not any(key.startswith("CATALOG_SECRET_") for key in store.rows)


async def test_import_preserves_unquoted_pricing_schedule_dates(tmp_path):
    from serving.pricing import PricingSchedule

    store = await configure()
    manifest = tmp_path / "legacy.yaml"
    manifest.write_text(
        "schema_version: 1\ndistribution: {id: imported}\npaths: {models: models.yaml}\n"
    )
    (tmp_path / "models.yaml").write_text(
        "models:\n- id: scheduled\n  provider: vllm\n  base_url: http://localhost:9/v1\n"
        "  pricing_schedule:\n"
        "    effective_at: '2026-11-01T00:00:00Z'\n"
        "    excluded_dates: [2026-12-25]\n"
        "    default: {prompt: '0.15'}\n    windows: []\n"
    )
    await import_legacy(manifest)
    raw = json.loads(store.rows["MODEL_CATALOG"].value)["models"][0]["pricing_schedule"]
    assert raw["excluded_dates"] == ["2026-12-25"]
    assert PricingSchedule.from_raw(raw).excluded_dates


DARK_MANIFEST = (
    "schema_version: 1\ndistribution: {id: dark}\n"
    "features: {public_signup: false, rag: false}\npaths: {alerts: alerts.yaml}\n"
)
ALERTS = "rules:\n  client_error_burst:\n    threshold_count: 7\n"


@pytest.mark.parametrize("mode", [None, "dark", "activ"])
async def test_a_manifest_that_was_never_in_force_is_not_imported(
    tmp_path, monkeypatch, caplog, mode
):
    set_environment(monkeypatch, DISTRIBUTION_CONFIG_MODE=mode)
    store = await configure()
    manifest = tmp_path / "legacy.yaml"
    manifest.write_text(DARK_MANIFEST)
    (tmp_path / "alerts.yaml").write_text(ALERTS)
    with caplog.at_level("WARNING", logger="serving.config.import_legacy"):
        assert await import_legacy(manifest) == []
    assert store.rows == {}
    assert "never in force" in caplog.text
    assert "--include-dark" in caplog.text


async def test_a_dark_manifest_is_imported_when_asked_to(tmp_path, monkeypatch):
    set_environment(monkeypatch, DISTRIBUTION_CONFIG_MODE="dark")
    store = await configure()
    manifest = tmp_path / "legacy.yaml"
    manifest.write_text(DARK_MANIFEST)
    (tmp_path / "alerts.yaml").write_text(ALERTS)
    saved = await import_legacy(manifest, include_dark=True)
    assert saved == ["ALERT_RULES", "SITE_CONFIGURATION"]
    site = json.loads(store.rows["SITE_CONFIGURATION"].value)
    assert site["features"] == {"public_signup": False, "rag": False}


@pytest.mark.parametrize("mode", ["active", "dark"])
async def test_the_alert_rules_the_environment_named_are_imported(tmp_path, monkeypatch, mode):
    # ALERTS_CONFIG_PATH won over the manifest's file in either mode.
    rules = tmp_path / "deployed-alerts.yaml"
    rules.write_text(ALERTS)
    set_environment(monkeypatch, DISTRIBUTION_CONFIG_MODE=mode, ALERTS_CONFIG_PATH=str(rules))
    store = await configure()
    manifest = tmp_path / "legacy.yaml"
    manifest.write_text(DARK_MANIFEST)
    (tmp_path / "alerts.yaml").write_text("rules:\n  client_error_burst:\n    threshold_count: 1\n")
    saved = await import_legacy(manifest)
    assert "ALERT_RULES" in saved
    stored = json.loads(store.rows["ALERT_RULES"].value)
    assert stored["rules"]["client_error_burst"]["threshold_count"] == 7


async def test_alert_rules_import_without_a_manifest(tmp_path):
    store = await configure()
    rules = tmp_path / "alerts.yaml"
    rules.write_text(ALERTS)
    assert await import_legacy(alerts=rules) == ["ALERT_RULES"]
    assert json.loads(store.rows["ALERT_RULES"].value)["rules"]["client_error_burst"] == {
        "threshold_count": 7
    }


async def test_a_file_the_previous_release_never_found_is_skipped(tmp_path, monkeypatch, caplog):
    set_environment(monkeypatch, ALERTS_CONFIG_PATH=str(tmp_path / "absent.yaml"))
    store = await configure()
    with caplog.at_level("WARNING", logger="serving.config.import_legacy"):
        assert await import_legacy() == []
    assert "absent.yaml does not exist" in caplog.text
    assert store.rows == {}
    # A file named on the command line must exist.
    with pytest.raises(ValueError, match="Cannot read"):
        await import_legacy(alerts=tmp_path / "absent.yaml")


async def test_an_invalid_document_is_refused_by_name_and_nothing_is_stored(tmp_path):
    store = await configure()
    manifest = tmp_path / "legacy.yaml"
    manifest.write_text("schema_version: 1\ndistribution: {id: imported}\n")
    rules = tmp_path / "alerts.yaml"
    rules.write_text("rules:\n  client_error_burst:\n    threshold_count: many\n")
    with pytest.raises(app_config.ConfigUpdateError) as refused:
        await import_legacy(manifest, alerts=rules)
    assert refused.value.detail.startswith("ALERT_RULES: /rules/client_error_burst/")
    assert "many" not in refused.value.detail
    assert store.rows == {}
