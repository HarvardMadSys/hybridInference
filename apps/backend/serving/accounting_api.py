"""Native accounting amounts alongside explicitly USD compatibility contracts."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, ClassVar, Literal

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
        "budget_daily_usd",
        "budget_monthly_usd",
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


#: The server-owned financial dictionaries a response may nest.
_FINANCIAL_DICTIONARIES = ("today", "this_month", "new_values")


def _usd_shape(value: Mapping[str, Any]) -> dict[str, Any]:
    """*value* without the native aliases of its ``*_usd`` entries, nor ``currency``."""
    aliases = {key.removesuffix("_usd") for key in MONEY_FIELDS.intersection(value)}
    return {k: v for k, v in value.items() if k not in aliases and k != "currency"}


class MoneyResponse(BaseModel):
    """A monetary response with additive native fields and truthful USD aliases.

    On a USD ledger a response serializes exactly its USD fields: no
    ``currency`` and no native aliases, the shape its contract had before a
    deployment could keep another currency (``usd_shape_unchanged``, which a
    response new enough to have no earlier shape may turn off). A non-USD
    response always carries both.
    """

    usd_shape_unchanged: ClassVar[bool] = True

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
        if type(self).usd_shape_unchanged and not get_accounting_policy().native:
            for legacy in MONEY_FIELDS.intersection(type(self).model_fields):
                native = legacy.removesuffix("_usd")
                if native in type(self).model_fields:
                    result.pop(native, None)
            result.pop("currency", None)
            # The financial dictionaries keep their USD shape too: no native
            # alias beside a ``*_usd`` entry, and no currency.
            for key in _FINANCIAL_DICTIONARIES:
                if isinstance(result.get(key), dict):
                    result[key] = _usd_shape(result[key])
            return result
        # These fields are server-owned financial dictionaries. Never rewrite
        # arbitrary user metadata, prompts, or provider-native cost objects.
        for key in _FINANCIAL_DICTIONARIES:
            if key in result:
                result[key] = monetary_payload(result[key], recursive=False)
        return result


class NativeOnlyFields(BaseModel):
    """A response whose ``native_fields`` exist only on a non-USD ledger.

    For a response that is not a :class:`MoneyResponse` (its amounts are not
    ``*_usd`` pairs) but grew native fields: on a USD ledger they are left out,
    so the response keeps the shape its contract had.
    """

    native_fields: ClassVar[tuple[str, ...]] = ()

    @model_serializer(mode="wrap")
    def _native_only(self, handler: Any) -> Any:
        result = handler(self)
        if isinstance(result, dict) and not get_accounting_policy().native:
            for name in type(self).native_fields:
                result.pop(name, None)
        return result


def native_only(**fields: Any) -> dict[str, Any]:
    """*fields* on a non-USD ledger, nothing on a USD one (for a dict response)."""
    return {**fields, "currency": accounting_currency()} if get_accounting_policy().native else {}


class MoneyInput(BaseModel):
    """Native writes declare their currency; legacy writes remain explicitly USD.

    On a USD ledger a native field is only another name for its ``*_usd``
    field: its value is passed on unchanged, and validated exactly as the
    ``*_usd`` field would validate it.

    A subclass whose USD field is stored at a fixed scale names it in
    ``usd_places``: the USD equivalent derived from a native amount is rounded
    (half up, as PostgreSQL rounds ``numeric``) to that many places, so it
    passes the field's own ``decimal_places`` and matches what is stored.
    """

    usd_places: ClassVar[dict[str, int]] = {}

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
            if not policy.native:
                result[legacy] = raw
                continue
            usd = None if raw is None else policy.to_usd(raw)
            places = cls.usd_places.get(legacy)
            if usd is not None and places is not None:
                usd = usd.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
            result[legacy] = usd
        if has_prices:
            if "pricing" in result:
                raise ValueError("Specify either prices or pricing, not both")
            prices = result["prices"]
            if not isinstance(prices, Mapping):
                raise ValueError("prices must be a mapping")
            result["pricing"] = (
                {key: str(policy.to_usd(raw)) for key, raw in prices.items()}
                if policy.native
                else dict(prices)
            )
        return result


def native_quota_updates(payload: BaseModel) -> dict[str, Any]:
    """Keep the submitted native precision when storing compatibility USD too.

    Nothing on a USD ledger: its record is the ``*_usd`` column, and the
    trigger derives the native one from what was stored there.
    """
    if not get_accounting_policy().native:
        return {}
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
