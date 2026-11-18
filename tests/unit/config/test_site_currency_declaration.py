"""The ledger currency a deployment declares lives in SITE_CONFIGURATION.

The previous release read ``accounting.currency`` from its distribution
manifest; this one reads the same section from the database document the
legacy importer fills from that manifest. A running gateway refuses a document
that no longer matches its ledger, and startup compares the two.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from serving import accounting
from serving.accounting import AccountingPolicy, bind_accounting_policy
from serving.config import app_config, documents
from serving.config.distribution import DistributionConfig
from serving.config.import_legacy import import_legacy
from serving.config.settings import get_settings
from tests.fixtures.app_config_store import FakeAppConfigStore

POLICY = AccountingPolicy("CNY", Decimal("6.7180"), "2026-11-05", "synthetic fixed rate")
CNY_SITE = json.dumps(
    {"schema_version": 1, "distribution": {"id": "cny"}, "accounting": {"currency": "CNY"}}
)
USD_SITE = json.dumps({"schema_version": 1, "distribution": {"id": "cny"}})


@pytest.fixture(autouse=True)
def unbound():
    bind_accounting_policy(None)
    yield
    bind_accounting_policy(None)


def test_an_offline_tool_may_store_either_declaration():
    # No policy is bound outside the gateway: the next startup compares.
    documents.validate("SITE_CONFIGURATION", CNY_SITE)
    documents.validate("SITE_CONFIGURATION", USD_SITE)


def test_a_cny_gateway_refuses_a_document_that_drops_or_changes_its_currency():
    bind_accounting_policy(POLICY)
    documents.validate("SITE_CONFIGURATION", CNY_SITE)
    for text in (USD_SITE, "{}", json.dumps({**json.loads(CNY_SITE), "accounting": {}})):
        with pytest.raises(documents.DocumentError, match="must be CNY"):
            documents.validate("SITE_CONFIGURATION", text)


def test_a_usd_gateway_refuses_a_cny_declaration():
    bind_accounting_policy(AccountingPolicy())
    documents.validate("SITE_CONFIGURATION", USD_SITE)
    with pytest.raises(documents.DocumentError, match="must be USD"):
        documents.validate("SITE_CONFIGURATION", CNY_SITE)


def test_an_unknown_currency_is_refused_by_the_schema():
    with pytest.raises(documents.DocumentError):
        documents.validate(
            "SITE_CONFIGURATION",
            json.dumps({**json.loads(CNY_SITE), "accounting": {"currency": "EUR"}}),
        )
    assert accounting.get_accounting_policy().currency == "USD"


async def test_the_legacy_import_carries_the_manifest_currency(tmp_path, monkeypatch):
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    for key in ("MODELS_CONFIG_PATH", "ROUTING_CONFIG_PATH", "ALERTS_CONFIG_PATH"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    store = FakeAppConfigStore([])
    await app_config.load(store, bootstrap=False)
    app_config.attach(store)
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(
        "schema_version: 1\n"
        "distribution: {id: example-cny, display_name: Synthetic CNY}\n"
        "accounting: {currency: CNY}\n"
    )
    saved = await import_legacy(manifest)
    assert "SITE_CONFIGURATION" in saved
    stored = json.loads(store.rows["SITE_CONFIGURATION"].value)
    assert stored["accounting"] == {"currency": "CNY"}
    assert DistributionConfig.model_validate(stored).accounting.currency == "CNY"
