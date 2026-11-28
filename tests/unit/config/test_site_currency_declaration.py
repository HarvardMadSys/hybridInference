"""The ledger currency a deployment declares lives in SITE_CONFIGURATION.

A running gateway reads ``accounting.currency`` from that database document,
refuses a document that no longer matches its ledger, and startup compares
the two.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from serving import accounting
from serving.accounting import AccountingPolicy, bind_accounting_policy
from serving.config import documents

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
