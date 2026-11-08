"""Database documents, neutral startup and portable configuration backups."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from routing.routers import FixedRouter
from serving.config import app_config
from serving.config.distribution import get_active_distribution_config
from serving.config.documents import DocumentError, get, separate_secrets, validate
from serving.servers.routers import site_config
from tests.fixtures.app_config_store import FakeAppConfigStore


@pytest.fixture
def configuration(monkeypatch):
    for key in (
        "MODEL_CATALOG",
        "ROUTING_POLICY",
        "ALERT_RULES",
        "SITE_CONFIGURATION",
        "SITE_BRANDING",
    ):
        monkeypatch.delenv(key, raising=False)
    app_config.reset_state()
    yield
    app_config.reset_state()


async def load(rows=()):
    store = FakeAppConfigStore(list(rows))
    await app_config.load(store, bootstrap=False)
    app_config.attach(store)
    return store


async def test_no_application_file_is_a_startup_fallback(configuration, monkeypatch, tmp_path):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text("schema_version: 1\ndistribution: {id: unexpected}\n")
    models = tmp_path / "models.yaml"
    models.write_text("models: [{id: unexpected}]\n")
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(models))
    await load()
    assert get("models") == {}
    assert get_active_distribution_config() is None
    response = await site_config._deployment_state(FixedRouter())
    assert response["configuration"]["incomplete"] is True
    assert (await site_config.get_site_config())["distribution"]["id"] == "neutral"


async def test_document_validation_refuses_inline_secrets_without_writing(configuration):
    store = await load()
    text = json.dumps({"models": [{"id": "one", "api_key": "never-publish"}]})
    with pytest.raises(app_config.ConfigUpdateError):
        await app_config.update({"MODEL_CATALOG": text}, updated_by="admin")
    assert "MODEL_CATALOG" not in store.rows
    assert "never-publish" not in str(app_config.describe())


async def test_catalog_and_referenced_credentials_round_trip_through_backup(configuration):
    document, secrets = separate_secrets(
        {
            "models": [
                {
                    "id": "one",
                    "name": "One",
                    "provider": "vllm",
                    "base_url": "http://localhost:9/v1",
                    "api_key": "private-provider-key",
                    "route": [{"kind": "vllm"}],
                }
            ]
        },
        "models",
    )
    await load()
    await app_config.update(
        {"MODEL_CATALOG": json.dumps(document), **secrets},
        dict.fromkeys(secrets, True),
        updated_by="admin",
    )
    public = await app_config.export_rows()
    assert "private-provider-key" not in json.dumps(public, default=str)
    assert any(item["key"] == "MODEL_CATALOG" and item["value"] for item in public)
    complete = await app_config.export_rows(include_secret_values=True)
    assert "private-provider-key" in json.dumps(complete, default=str)
    destination = FakeAppConfigStore()
    await app_config.load(destination, bootstrap=False)
    app_config.attach(destination)
    await app_config.restore_rows(complete, updated_by="restore-test")
    assert destination.rows["MODEL_CATALOG"].value == json.dumps(document)
    assert destination.rows[next(iter(secrets))].value == "private-provider-key"


async def test_site_policy_updates_invalidate_public_snapshot(configuration):
    await load()
    assert get_active_distribution_config() is None
    value = {
        "schema_version": 1,
        "distribution": {"id": "configured"},
        "features": {"public_signup": False},
    }
    await app_config.update({"SITE_CONFIGURATION": json.dumps(value)}, updated_by="admin")
    assert get_active_distribution_config().features.public_signup is False
    value["distribution"]["id"] = "changed"
    await app_config.update({"SITE_CONFIGURATION": json.dumps(value)}, updated_by="admin")
    assert get_active_distribution_config().distribution.id == "changed"


async def test_branding_can_be_configured_without_a_site_document(configuration):
    await load()
    root = Path(__file__).resolve().parents[3]
    await app_config.update(
        {"SITE_BRANDING": (root / "config/examples/branding.example.json").read_text()},
        updated_by="admin",
    )
    response = await site_config.get_site_config()
    assert response["distribution"]["id"] == "neutral"
    assert response["branding"]["organization"]["name"] == "Example Organization"


@pytest.mark.parametrize(
    "key,text,reason",
    [
        ("MODEL_CATALOG", "models: []", "valid JSON object"),
        ("MODEL_CATALOG", '[{"models": []}]', "must be a JSON object"),
        ("MODEL_CATALOG", '{"models": [{"id": "no-adapter"}]}', "kind is required"),
        (
            "MODEL_CATALOG",
            '{"models": [{"id": "one", "route": [{"kind": "vllm", "weight": -1}]}]}',
            r"weight must be a number, 0 or more \(model 'one', route 1\)",
        ),
        (
            "SITE_CONFIGURATION",
            '{"schema_version":1,"distribution":{"id":"x"},"paths":{"models":"models.yaml"}}',
            "file paths are retired",
        ),
        ("SITE_BRANDING", '{"unexpected_secret": "secret"}', "Field required"),
    ],
)
def test_invalid_documents_are_refused(key, text, reason):
    with pytest.raises(DocumentError, match=reason):
        validate(key, text)


def test_import_secret_names_never_depend_on_credential_values():
    first, first_secrets = separate_secrets(
        {"headers": {"Authorization": "Bearer first"}}, "models"
    )
    second, second_secrets = separate_secrets(
        {"headers": {"Authorization": "Bearer second"}}, "models"
    )
    assert first == second
    assert list(first_secrets) == list(second_secrets)


@pytest.mark.parametrize(
    "key,filename",
    [
        ("SITE_CONFIGURATION", "distribution.example.json"),
        ("SITE_BRANDING", "branding.example.json"),
        ("MODEL_CATALOG", "models.openrouter.json"),
        ("MODEL_CATALOG", "models.routewise.json"),
        ("MODEL_CATALOG", "models.routewise.quota.json"),
        ("ROUTING_POLICY", "routing.minimal.json"),
    ],
)
def test_application_examples_can_be_saved_in_admin(key, filename):
    root = Path(__file__).resolve().parents[3]
    validate(key, (root / "config/examples" / filename).read_text())


def test_token_limits_are_numbers_rather_than_credentials():
    value = {
        "models": [
            {
                "id": "limits",
                "provider": "vllm",
                "router": "routewise",
                "router_params": {"output_default_tokens": 512},
                "extra_body": {"max_tokens": 1024},
            }
        ]
    }
    document, secrets = separate_secrets(value, "models")
    assert document == value and not secrets
    validate("MODEL_CATALOG", json.dumps(document))


async def test_alert_documents_keep_setting_reference_expansion(configuration):
    from serving.observability.alert_config import alert_config_from_data

    store = await load()
    await app_config.update({"FAILED_REQUEST_ALERT_THRESHOLD": 123}, updated_by="admin")
    await app_config.load(store, bootstrap=False)
    value = {
        "rules": {"client_error_burst": {"threshold_count": "${FAILED_REQUEST_ALERT_THRESHOLD}"}}
    }
    validate("ALERT_RULES", json.dumps(value))
    assert alert_config_from_data(value).rules.client_error_burst.threshold_count == 123


async def test_migrated_nested_credentials_reach_the_adapter_without_changing_metadata(
    configuration,
):
    from serving.servers.registry import register_from_models_document

    document, secrets = separate_secrets(
        {
            "models": [
                {
                    "id": "nested",
                    "name": "Nested",
                    "provider": "vllm",
                    "base_url": "http://localhost:9/v1",
                    "extra_body": {"custom_token": "legacy-private-token", "literal": "${LITERAL}"},
                }
            ]
        },
        "models",
    )
    store = await load()
    await app_config.update(
        {"MODEL_CATALOG": json.dumps(document), **secrets},
        dict.fromkeys(secrets, True),
        updated_by="admin",
    )
    await app_config.load(store, bootstrap=False)
    router = FixedRouter()
    register_from_models_document(router, document)
    body = router.routes["nested"].adapters[0][0].config.extra_body
    assert body == {"custom_token": "legacy-private-token", "literal": "${LITERAL}"}
