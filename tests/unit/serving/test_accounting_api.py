"""Native money contracts preserve precision and the old USD meaning."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from serving import accounting
from serving.accounting import AccountingPolicy
from serving.accounting_api import increment_native_cost, monetary_payload, native_quota_updates
from serving.schemas_admin import (
    CreateAPIKeyRequest,
    UpdateAPIKeyResponse,
    UpdateUserRequest,
    UserCostHistoryPoint,
)


@pytest.fixture(autouse=True)
def cny_policy(monkeypatch: pytest.MonkeyPatch) -> AccountingPolicy:
    policy = AccountingPolicy(
        currency="CNY",
        usd_to_currency_rate=Decimal("6.7180"),
        rate_date="2026-10-23",
        rate_source="https://example.test/fixed-rate",
    )
    monkeypatch.setattr(accounting, "_policy", policy)
    return policy


def test_native_quota_precision_survives_usd_compatibility_projection(cny_policy) -> None:
    amount = Decimal("1000.123456789012")
    payload = CreateAPIKeyRequest(user_id="native-user", quota_daily_cost=amount, currency="CNY")
    assert payload.quota_daily_cost_usd == amount / cny_policy.usd_to_currency_rate
    assert native_quota_updates(payload) == {"quota_daily_cost_amount": amount}
    serialized = payload.model_dump(exclude_unset=True)
    assert "currency" not in serialized
    assert "quota_daily_cost" not in serialized
    assert serialized["quota_daily_cost_usd"] == payload.quota_daily_cost_usd


@pytest.mark.parametrize("currency", [None, "USD", "EUR"])
def test_native_quota_requires_current_currency(currency) -> None:
    with pytest.raises(ValidationError):
        UpdateUserRequest(quota_daily_cost="10", currency=currency)


@pytest.mark.parametrize("amount", [-1, True, "NaN", "Infinity", "-Infinity"])
def test_native_quota_rejects_invalid_money(amount) -> None:
    with pytest.raises(ValidationError):
        UpdateUserRequest(quota_daily_cost=amount, currency="CNY")


def test_native_and_legacy_quota_cannot_both_choose_the_amount() -> None:
    with pytest.raises(ValidationError, match="not both"):
        UpdateUserRequest(quota_daily_cost="10", quota_daily_cost_usd="10", currency="CNY")


def test_legacy_dollar_write_remains_dollars_on_cny_deployment() -> None:
    payload = UpdateUserRequest(quota_daily_cost_usd="10")
    assert payload.quota_daily_cost_usd == Decimal("10")
    assert native_quota_updates(payload) == {}


def test_native_null_monthly_quota_preserves_clear_operation() -> None:
    payload = UpdateUserRequest(quota_monthly_cost=None, currency="CNY")
    assert payload.quota_monthly_cost_usd is None
    assert native_quota_updates(payload) == {"quota_monthly_cost_amount": None}


def test_history_response_prefers_primary_amount_over_rounded_legacy_value() -> None:
    amount = Decimal("0.123456789012")
    row = UserCostHistoryPoint.model_validate(
        {"day": "2026-10-23", "cost_usd": "0.018", "cost_amount": amount, "requests": 1}
    )
    assert row.cost == amount
    rendered = row.model_dump(mode="json")
    assert Decimal(rendered["cost"]) == amount
    assert Decimal(rendered["cost_usd"]) == Decimal("0.018")
    assert rendered["currency"] == "CNY"


@pytest.mark.parametrize("field", ["currency", "accounting_currency"])
def test_response_rejects_stored_currency_inconsistent_with_the_ledger(field) -> None:
    with pytest.raises(ValidationError, match="accounting policy"):
        UserCostHistoryPoint.model_validate(
            {"day": "2026-10-23", "cost_usd": "1", "cost_amount": "2", "requests": 1, field: "USD"}
        )


def test_nested_projection_does_not_change_provider_invoice_or_unknown_cost() -> None:
    response = monetary_payload(
        {"requests": [{"cost_usd": None}, {"cost_usd": "1", "upstream_cost_usd": "0.8"}]}
    )
    unknown, charged = response["requests"]
    assert unknown["cost"] is None
    assert charged["cost"] == Decimal("6.7180")
    assert charged["cost_usd"] == "1"
    assert charged["upstream_cost_usd"] == "0.8"
    assert "upstream_cost" not in charged


def test_update_response_does_not_interpret_user_metadata_as_money() -> None:
    metadata = {"cost_usd": "unbilled", "nested": {"quota_usd": "custom"}}
    response = UpdateAPIKeyResponse(
        user_id="native-user",
        updated_fields=["metadata", "quota_daily_cost_usd"],
        new_values={"metadata": metadata, "quota_daily_cost_usd": "1"},
    )
    data = response.model_dump(mode="json")
    assert data["new_values"]["metadata"] == metadata
    assert Decimal(data["new_values"]["quota_daily_cost"]) == Decimal("6.7180")


def test_quota_alert_displays_accounting_currency(cny_policy) -> None:
    from serving.observability.alert_rules import _quota_card

    card = _quota_card("native-user", "free", 2, 1, None)
    assert card["currency"] == "CNY"
    assert card["spend"] == "¥13.44"
    assert card["quota"] == "¥6.72"
    assert card["over_by"] == "¥6.72"


@pytest.mark.asyncio
async def test_native_increment_uses_primary_ledger_without_round_trip() -> None:
    store = AsyncMock()
    amount = Decimal("0.000000123456")
    await increment_native_cost(store, "native-user", amount)
    store.increment_user_cost_amount.assert_awaited_once_with("native-user", amount)
    store.increment_user_cost.assert_not_awaited()


@pytest.mark.asyncio
async def test_usd_deployment_keeps_existing_store_contract(monkeypatch) -> None:
    monkeypatch.setattr(accounting, "_policy", AccountingPolicy())
    store = AsyncMock()
    await increment_native_cost(store, "old-user", Decimal("0.25"))
    store.increment_user_cost.assert_awaited_once_with("old-user", Decimal("0.25"))
    store.increment_user_cost_amount.assert_not_awaited()
