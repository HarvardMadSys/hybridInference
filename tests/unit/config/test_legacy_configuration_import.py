"""The explicit legacy importer never becomes a boot fallback or overwrites settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serving.config import app_config
from serving.config.import_legacy import import_legacy
from tests.fixtures.app_config_store import FakeAppConfigStore, row


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
