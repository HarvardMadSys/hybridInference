"""Application documents from the environment, the reasons Admin gives, and the audit log."""

from __future__ import annotations

import json

import pytest

from serving.config import app_config
from serving.config.app_config_registry import static_entries
from serving.config.distribution import DistributionConfigError, get_active_distribution_config
from serving.config.documents import get
from serving.config.settings import get_settings
from tests.fixtures.app_config_store import FakeAppConfigStore, row

INLINE = "inline-provider-credential"
INVALID_CATALOG = json.dumps({"models": [{"id": "one", "api_key": INLINE}]})
VALID_CATALOG = json.dumps({"models": [{"id": "one", "provider": "vllm", "api_key": "${ONE_KEY}"}]})


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """No registered setting set, application documents included."""
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    get_settings.cache_clear()


async def load(rows=(), *, bootstrap=False):
    store = FakeAppConfigStore(list(rows))
    await app_config.load(store, bootstrap=bootstrap)
    app_config.attach(store)
    return store


# --- the reason Admin gives ---------------------------------------------------


async def test_admin_names_why_a_document_is_refused_but_never_its_value():
    await load([row("ROUTING_POLICY", json.dumps({"timeout": -1}))])
    with pytest.raises(app_config.ConfigUpdateError) as refused:
        await app_config.update({"MODEL_CATALOG": INVALID_CATALOG}, updated_by="admin")
    assert refused.value.detail == (
        "MODEL_CATALOG: /models/0/api_key must reference a separate secret setting as ${NAME}."
    )
    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["ROUTING_POLICY"]["invalid"] == "/timeout value must be non-negative"


async def test_a_reason_never_repeats_a_reference_the_validator_expanded(monkeypatch):
    monkeypatch.setenv("PRIVATE_ENDPOINT", "private-endpoint-credential")
    await load()
    policy = {"local_deployment": [{"endpoint": "${PRIVATE_ENDPOINT}", "models": ["m"]}]}
    with pytest.raises(app_config.ConfigUpdateError) as refused:
        await app_config.update({"ROUTING_POLICY": json.dumps(policy)}, updated_by="admin")
    assert "/local_deployment/0/endpoint endpoint must start with http://" in refused.value.detail
    assert "private-endpoint-credential" not in refused.value.detail


async def test_a_stored_policy_naming_no_strategy_is_reported_instead_of_served():
    """Saved before the check existed, it failed every startup; now the default policy applies."""
    await load([row("ROUTING_POLICY", json.dumps({"default_router": "fixd"}))])

    assert get("routing") == {}
    entries = {entry["key"]: entry for entry in app_config.describe()["entries"]}
    assert entries["ROUTING_POLICY"]["invalid"].startswith(
        "/default_router names no routing strategy"
    )


# --- documents from the environment -------------------------------------------


async def test_an_invalid_environment_document_is_neither_imported_nor_served(monkeypatch, caplog):
    monkeypatch.setenv("MODEL_CATALOG", INVALID_CATALOG)
    with caplog.at_level("WARNING", logger="serving.config.app_config"):
        store = await load(bootstrap=True)
    assert "MODEL_CATALOG" not in store.rows
    assert "Not importing invalid MODEL_CATALOG from the environment" in caplog.text
    assert INLINE not in caplog.text
    # It counts as unset: never served, inline credential and all.
    assert app_config.config_value("MODEL_CATALOG") is None
    assert get("models") == {}


async def test_a_valid_environment_document_is_still_imported(monkeypatch):
    monkeypatch.setenv("MODEL_CATALOG", VALID_CATALOG)
    store = await load(bootstrap=True)
    assert store.rows["MODEL_CATALOG"].value == VALID_CATALOG
    assert get("models")["models"][0]["api_key"] == "${ONE_KEY}"


async def test_startup_refuses_an_invalid_environment_document_nothing_replaces(monkeypatch):
    monkeypatch.setenv("MODEL_CATALOG", INVALID_CATALOG)
    await load()
    with pytest.raises(app_config.ConfigBootstrapError) as refused:
        app_config.require_valid_environment()
    assert "MODEL_CATALOG: /models/0/api_key must reference" in str(refused.value)
    assert INLINE not in str(refused.value)


async def test_a_valid_stored_document_wins_over_an_invalid_environment_one(monkeypatch, caplog):
    monkeypatch.setenv("MODEL_CATALOG", INVALID_CATALOG)
    await load([row("MODEL_CATALOG", VALID_CATALOG)])
    with caplog.at_level("WARNING", logger="serving.config.app_config"):
        app_config.require_valid_environment()
    assert "Ignoring invalid MODEL_CATALOG from the environment" in caplog.text
    assert INLINE not in caplog.text
    assert get("models")["models"][0]["api_key"] == "${ONE_KEY}"


async def test_a_reset_cannot_put_an_invalid_environment_document_back_in_force(monkeypatch):
    monkeypatch.setenv("MODEL_CATALOG", INVALID_CATALOG)
    store = await load([row("MODEL_CATALOG", VALID_CATALOG)])
    with pytest.raises(app_config.ConfigUpdateError) as refused:
        await app_config.delete("MODEL_CATALOG", updated_by="admin")
    assert refused.value.status_code == 409
    assert "its environment value is not valid" in refused.value.detail
    assert INLINE not in refused.value.detail
    assert store.rows["MODEL_CATALOG"].value == VALID_CATALOG


def test_an_invalid_environment_site_document_closes_the_site_before_anything_loads(
    monkeypatch,
):
    # Counting it as unset would drop the restriction it was meant to carry.
    monkeypatch.setenv(
        "SITE_CONFIGURATION",
        json.dumps(
            {"schema_version": 1, "distribution": {"id": "x"}, "features": {"public-signup": False}}
        ),
    )
    with pytest.raises(DistributionConfigError):
        get_active_distribution_config()


# --- the audit log -------------------------------------------------------------


async def test_the_audit_log_withholds_a_refused_document_an_update_replaces():
    await load([row("MODEL_CATALOG", INVALID_CATALOG)])
    [change] = await app_config.update({"MODEL_CATALOG": VALID_CATALOG}, updated_by="admin")
    assert change.audit_details() == {"key": "MODEL_CATALOG", "changed": True}


async def test_the_audit_log_withholds_a_refused_document_a_reset_removes():
    await load([row("MODEL_CATALOG", INVALID_CATALOG)])
    change = await app_config.delete("MODEL_CATALOG", updated_by="admin")
    assert change.audit_details() == {"key": "MODEL_CATALOG", "changed": True}


async def test_the_audit_log_still_records_a_valid_document():
    await load([row("MODEL_CATALOG", VALID_CATALOG)])
    replacement = json.dumps({"models": []})
    [change] = await app_config.update({"MODEL_CATALOG": replacement}, updated_by="admin")
    assert change.audit_details() == {
        "key": "MODEL_CATALOG",
        "old_value": VALID_CATALOG,
        "new_value": replacement,
    }


# --- restoring a backup -------------------------------------------------------


async def test_a_restore_keeps_the_documents_and_credentials_a_backup_predates():
    store = await load(
        [
            row("MODEL_CATALOG", VALID_CATALOG),
            row("CATALOG_SECRET_0123456789ABCDEF0123", "lifted-credential", secret=True),
            row("ROUTING_POLICY", "", source="reset"),
            row("SITE_NAME", "Before"),
        ]
    )
    # Taken before the upgrade: it names no document and no lifted credential.
    summary = await app_config.restore_rows(
        [{"key": "SITE_NAME", "value": "After", "source": "admin"}], updated_by="restore"
    )
    assert store.rows["MODEL_CATALOG"].value == VALID_CATALOG
    assert store.rows["CATALOG_SECRET_0123456789ABCDEF0123"].value == "lifted-credential"
    assert store.rows["ROUTING_POLICY"].source == "reset"
    assert store.rows["SITE_NAME"].value == "After"
    assert summary.changed == ("SITE_NAME",)


async def test_a_restore_still_replaces_a_document_the_backup_names():
    store = await load([row("MODEL_CATALOG", VALID_CATALOG)])
    replacement = json.dumps({"models": []})
    await app_config.restore_rows(
        [{"key": "MODEL_CATALOG", "value": replacement, "source": "admin"}], updated_by="restore"
    )
    assert store.rows["MODEL_CATALOG"].value == replacement
