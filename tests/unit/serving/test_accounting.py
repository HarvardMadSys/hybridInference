"""Currency policy rejects ambiguous or nonfinite monetary units."""

from decimal import Decimal

import pytest

from serving.accounting import AccountingPolicy, load_accounting_policy, money_decimal


def test_native_to_usd_round_trip_uses_decimal():
    policy = AccountingPolicy("CNY", Decimal("6.7180"), "2026-10-23", "fixture")
    assert policy.from_usd("0.15") == Decimal("1.007700")
    assert policy.from_usd(policy.to_usd("10")) == Decimal("10")
    assert AccountingPolicy.from_dict(policy.as_dict()) == policy


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "-Infinity", "-1", None])
def test_invalid_money_is_rejected(value):
    with pytest.raises(ValueError):
        money_decimal(value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"currency": "RMB"},
        {"usd_to_currency_rate": Decimal("6.7")},
        {"currency": "CNY", "usd_to_currency_rate": Decimal("0")},
        {"currency": "CNY", "usd_to_currency_rate": Decimal("6.7")},
    ],
)
def test_invalid_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        AccountingPolicy(**kwargs)


@pytest.mark.asyncio
async def test_corrupt_persisted_policy_never_defaults_to_usd():
    class Store:
        async def get_setting(self, key):
            return {"value_type": "json", "value": '{"currency":"CNY"}'}

    with pytest.raises(ValueError):
        await load_accounting_policy(Store())


@pytest.mark.asyncio
async def test_cny_requires_matching_completed_migration():
    import json

    from serving.accounting import MIGRATION_KEY, POLICY_KEY

    policy = AccountingPolicy("CNY", Decimal("6.7180"), "2026-10-23", "fixture")

    class Store:
        def __init__(self):
            self.rows = {POLICY_KEY: {"value_type": "json", "value": json.dumps(policy.as_dict())}}

        async def get_setting(self, key):
            return self.rows.get(key)

    store = Store()
    with pytest.raises(ValueError, match="completed offline migration"):
        await load_accounting_policy(store)
    store.rows[MIGRATION_KEY] = {
        "value_type": "json",
        "value": json.dumps(AccountingPolicy().as_dict()),
    }
    with pytest.raises(ValueError, match="does not match"):
        await load_accounting_policy(store)
    store.rows[MIGRATION_KEY] = store.rows[POLICY_KEY]
    assert await load_accounting_policy(store) == policy
    assert await load_accounting_policy(None) == AccountingPolicy()
