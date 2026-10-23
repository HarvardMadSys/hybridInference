"""An immutable deployment accounting unit and explicit USD compatibility rate."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

POLICY_KEY = "accounting:policy"
MIGRATION_KEY = "accounting:migration"


def money_decimal(value: Any) -> Decimal:
    """Parse finite, nonnegative money without passing through binary float."""
    if isinstance(value, bool):
        raise ValueError("money must be a finite nonnegative number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("money must be a finite nonnegative number") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("money must be a finite nonnegative number")
    return result


@dataclass(frozen=True, slots=True)
class AccountingPolicy:
    """Describe one immutable deployment currency and its USD compatibility rate."""

    currency: Literal["USD", "CNY"] = "USD"
    usd_to_currency_rate: Decimal = Decimal("1")
    rate_date: str = ""
    rate_source: str = ""

    def __post_init__(self) -> None:
        """Reject unsupported units or undocumented conversion rates."""
        rate = money_decimal(self.usd_to_currency_rate)
        if self.currency not in {"USD", "CNY"} or rate <= 0:
            raise ValueError("accounting requires USD or CNY and a positive rate")
        if self.currency == "USD" and rate != 1:
            raise ValueError("USD accounting requires an identity conversion rate")
        if self.currency == "CNY":
            try:
                parsed = dt.date.fromisoformat(self.rate_date)
            except (ValueError, TypeError) as exc:
                raise ValueError("CNY accounting requires an ISO rate date") from exc
            if parsed.isoformat() != self.rate_date or not self.rate_source.strip():
                raise ValueError("CNY accounting requires a rate date and source")
        object.__setattr__(self, "usd_to_currency_rate", rate)

    def from_usd(self, value: Any) -> Decimal:
        """Convert a true USD value into the deployment accounting unit."""
        return money_decimal(value) * self.usd_to_currency_rate

    def to_usd(self, value: Any) -> Decimal:
        """Return the USD equivalent for compatibility readers."""
        return money_decimal(value) / self.usd_to_currency_rate

    def as_dict(self) -> dict[str, str | int]:
        """Serialize a versioned policy without losing decimal precision."""
        return {
            "version": 1,
            "currency": self.currency,
            "usd_to_currency_rate": str(self.usd_to_currency_rate),
            "rate_date": self.rate_date,
            "rate_source": self.rate_source,
        }

    @classmethod
    def from_dict(cls, value: Any) -> AccountingPolicy:
        """Validate and load an exact versioned policy record."""
        required = {"version", "currency", "usd_to_currency_rate", "rate_date", "rate_source"}
        if not isinstance(value, dict) or set(value) != required or value["version"] != 1:
            raise ValueError("unsupported accounting policy")
        if any(not isinstance(value[key], str) for key in required - {"version"}):
            raise ValueError("invalid accounting policy fields")
        return cls(**{key: value[key] for key in required - {"version"}})


_policy = AccountingPolicy()


def get_accounting_policy() -> AccountingPolicy:
    """Return the policy bound at application startup."""
    return _policy


def bind_accounting_policy(policy: AccountingPolicy | None) -> None:
    """Bind the validated deployment policy, or reset to the USD default."""
    global _policy
    if policy is not None and not isinstance(policy, AccountingPolicy):
        raise TypeError("expected AccountingPolicy")
    _policy = policy or AccountingPolicy()


async def load_accounting_policy(store: Any) -> AccountingPolicy:
    """Absent configuration is legacy USD; corrupt configuration fails closed."""
    if store is None:
        return AccountingPolicy()
    row = await store.get_setting(POLICY_KEY)
    if row is None:
        return AccountingPolicy()
    if row.get("value_type") != "json":
        raise ValueError("accounting policy must be stored as JSON")
    raw = row["value"]
    policy = AccountingPolicy.from_dict(json.loads(raw) if isinstance(raw, str) else raw)
    if policy.currency != "USD":
        marker = await store.get_setting(MIGRATION_KEY)
        if marker is None or marker.get("value_type") != "json":
            raise ValueError("CNY accounting requires a completed offline migration")
        marker_raw = marker["value"]
        completed = AccountingPolicy.from_dict(
            json.loads(marker_raw) if isinstance(marker_raw, str) else marker_raw
        )
        if completed != policy:
            raise ValueError("Accounting migration does not match the persisted policy")
    return policy
