"""Application documents from the environment, the reasons Admin gives, and the audit log."""

from __future__ import annotations

import json

import pytest

from serving.config import app_config
from serving.config.app_config_registry import static_entries
from serving.config.settings import get_settings
from tests.fixtures.app_config_store import FakeAppConfigStore, row

INLINE = "inline-provider-credential"
INVALID_CATALOG = json.dumps({"models": [{"id": "one", "api_key": INLINE}]})


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
