"""Native accounting amounts alongside explicitly USD compatibility contracts."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_serializer, model_validator

from serving.accounting import get_accounting_policy

Currency = Literal["USD", "CNY"]

# Provider invoices and RouteWise diagnostics retain their own USD semantics.
MONEY_FIELDS = frozenset(
    {
        "avg_prior_7d_usd",
        "cost_usd",
        "total_cost_usd",
        "today_cost_usd",
        "spent_usd",
        "quota_usd",
        "remaining_usd",
        "daily_limit_usd",
        "monthly_limit_usd",
        "spent_today_usd",
        "spent_month_usd",
        "remaining_today_usd",
        "quota_daily_cost_usd",
        "quota_monthly_cost_usd",
        "quota_daily_usd",
        "quota_monthly_usd",
        "quota_remaining_usd",
        "quota_remaining_cost_usd",
        "usage_today_usd",
        "usage_month_usd",
        "usage_alltime_usd",
        "min_spend_today_usd",
    }
)


def accounting_currency() -> Currency:
    """Return the immutable unit selected during gateway startup."""
    return get_accounting_policy().currency


def native_amount(value: Any) -> Decimal | None:
    """Project a signed USD report value into the accounting unit."""
    if value is None:
        return None
    number = Decimal(str(value))
    if isinstance(value, bool) or not number.is_finite():
        raise ValueError("Money must be finite")
    return number * get_accounting_policy().usd_to_currency_rate


def monetary_payload(value: Any, *, recursive: bool = True) -> Any:
    """Project compatibility USD aggregates using the immutable ledger rate.

    Existing native fields take precedence: primary ledger values must not be
    overwritten by a round trip through a compatibility amount.
    """
    if isinstance(value, list):
        return [monetary_payload(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {key: monetary_payload(item) if recursive else item for key, item in value.items()}
    money_keys = MONEY_FIELDS.intersection(result)
    for key in money_keys:
        native = key.removesuffix("_usd")
        if result.get(f"{native}_amount") is not None:
            result[native] = result[f"{native}_amount"]
        else:
            result.setdefault(native, native_amount(result[key]))
    if money_keys:
        result.setdefault("currency", accounting_currency())
    return result


class MoneyResponse(BaseModel):
    """A monetary response with additive native fields and truthful USD aliases."""

    currency: Currency = Field(default_factory=accounting_currency)

    @model_validator(mode="before")
    @classmethod
    def populate_native_amounts(cls, value: Any) -> Any:
        """Prefer stored primary amounts and reject inconsistent currency tags."""
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        declared = result.get("accounting_currency") or result.get("currency")
        if declared is not None and declared != accounting_currency():
            raise ValueError("Stored monetary currency does not match the accounting policy")
        for legacy in MONEY_FIELDS.intersection(cls.model_fields):
            native = legacy.removesuffix("_usd")
            if native in cls.model_fields and native not in result:
                if result.get(f"{native}_amount") is not None:
                    result[native] = result[f"{native}_amount"]
                elif legacy in result:
                    result[native] = native_amount(result[legacy])
        return result

    @model_validator(mode="after")
    def populate_default_amounts(self) -> MoneyResponse:
        """Populate native aliases for fields supplied by model defaults."""
        for legacy in MONEY_FIELDS.intersection(type(self).model_fields):
            native = legacy.removesuffix("_usd")
            if native in type(self).model_fields and native not in self.model_fields_set:
                setattr(self, native, native_amount(getattr(self, legacy)))
        return self

    @model_serializer(mode="wrap")
    def serialize_amounts(self, handler: Any) -> Any:
        """Add native aliases to known financial dictionaries only."""
        result = handler(self)
        # These fields are server-owned financial dictionaries. Never rewrite
        # arbitrary user metadata, prompts, or provider-native cost objects.
        for key in ("today", "this_month", "new_values"):
            if key in result:
                result[key] = monetary_payload(result[key], recursive=False)
        return result


class MoneyInput(BaseModel):
    """Native writes declare their currency; legacy writes remain explicitly USD."""

    currency: Currency | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_native_amounts(cls, value: Any) -> Any:
        """Validate native writes and derive their legacy USD equivalents."""
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        policy = get_accounting_policy()
        native_keys = {
            key.removesuffix("_usd"): key
            for key in MONEY_FIELDS.intersection(cls.model_fields)
            if key.removesuffix("_usd") in result
        }
        has_prices = "prices" in result and "pricing" in cls.model_fields
        if (native_keys or has_prices) and result.get("currency") != policy.currency:
            raise ValueError("Amount currency does not match the accounting currency; reload")
        for native, legacy in native_keys.items():
            if legacy in result:
                raise ValueError(f"Specify either {native} or {legacy}, not both")
            raw = result[native]
            result[legacy] = None if raw is None else policy.to_usd(raw)
        if has_prices:
            if "pricing" in result:
                raise ValueError("Specify either prices or pricing, not both")
            prices = result["prices"]
            if not isinstance(prices, Mapping):
                raise ValueError("prices must be a mapping")
            result["pricing"] = {key: str(policy.to_usd(raw)) for key, raw in prices.items()}
        return result


async def increment_native_cost(store: Any, user_id: str, amount: Any) -> None:
    """Use the primary ledger, retaining the USD-only store interface for USD."""
    if accounting_currency() == "USD":
        await store.increment_user_cost(user_id, amount)
    else:
        await store.increment_user_cost_amount(user_id, amount)


def native_quota_updates(payload: BaseModel) -> dict[str, Any]:
    """Keep the submitted native precision when storing compatibility USD too."""
    return {
        f"{name}_amount": getattr(payload, name)
        for name in ("quota_daily_cost", "quota_monthly_cost")
        if name in payload.model_fields_set
    }


def primary_amounts(row: Mapping[str, Any]) -> dict[str, Any]:
    """Carry primary values through explicit response constructors."""
    return {
        key.removesuffix("_amount"): value
        for key, value in row.items()
        if key.endswith("_amount")
        and f"{key.removesuffix('_amount')}_usd" in MONEY_FIELDS
        and value is not None
    }
