"""The deployment's ledger currency, and every rule its money follows.

A deployment keeps one ledger in one currency (:class:`AccountingPolicy`),
chosen once by the offline migration (``ops/accounting_currency.py``) and
immutable after it. USD is the default: a database that never ran the
migration has no stored policy and keeps exactly the ledger it always had.

The policy is the single place that knows how the currency changes the
arithmetic. Business code asks it, never compares a currency code:

- **Where the ledger's record is** (:attr:`AccountingPolicy.native`). A USD
  ledger's record is the ``*_usd`` columns it has always written; any other
  ledger's is the native ``*_amount`` columns beside them, and its ``*_usd``
  columns hold USD equivalents the ``accounting_sync_amounts`` triggers keep.
- **The arithmetic** (:meth:`~AccountingPolicy.number`). A USD ledger computes
  in binary floats, exactly as before other ledgers existed, so its charges do
  not change by a single rounding; a native ledger computes in decimals.
- **The quantum** (:attr:`~AccountingPolicy.quantum`,
  :meth:`~AccountingPolicy.quantize`): what one increment of the counters and
  the shared reservations is rounded to, half away from zero as PostgreSQL
  rounds ``numeric``. USD six places (``DECIMAL(12, 6)`` counters); native
  twelve (``NUMERIC(24, 12)``), so a price far below a cent per token still
  accumulates.
- **Conversions** (:meth:`~AccountingPolicy.in_ledger`,
  :meth:`~AccountingPolicy.as_usd`): one fixed rate, one direction per
  purpose. USD-denominated constants and tariffs come into the ledger at the
  rate; USD compatibility values are derived from ledger amounts, never the
  reverse. On a USD ledger both are the identity, type included.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

POLICY_KEY = "accounting:policy"
MIGRATION_KEY = "accounting:migration"

#: The currencies a ledger may keep. USD is also the compatibility currency:
#: every ``*_usd`` contract is stated in it, and the policy's rate is from it.
Currency = Literal["USD", "CNY"]
USD: Currency = "USD"

#: How a message names an amount in each currency.
SYMBOLS: dict[str, str] = {"USD": "$", "CNY": "¥"}

#: What a USD ledger rounds each counter increment to: its ``DECIMAL(12, 6)``.
USD_QUANTUM = Decimal("0.000001")
#: What a native ledger rounds each increment to: its ``NUMERIC(24, 12)``.
NATIVE_QUANTUM = Decimal("0.000000000001")

#: A gate's limit or spend: a float on a USD ledger, a decimal on a native one.
Money = float | Decimal


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
    """One immutable deployment currency and its USD compatibility rate.

    ``usd_to_currency_rate`` is units of ``currency`` per USD, fixed when the
    ledger was migrated, with the date and the source the operator recorded.
    """

    currency: Currency = USD
    usd_to_currency_rate: Decimal = Decimal("1")
    rate_date: str = ""
    rate_source: str = ""

    def __post_init__(self) -> None:
        """Reject unsupported units or undocumented conversion rates."""
        rate = money_decimal(self.usd_to_currency_rate)
        if self.currency not in SYMBOLS or rate <= 0:
            raise ValueError("accounting requires USD or CNY and a positive rate")
        if self.currency == USD and rate != 1:
            raise ValueError("USD accounting requires an identity conversion rate")
        if self.currency != USD:
            try:
                parsed = dt.date.fromisoformat(self.rate_date)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{self.currency} accounting requires an ISO rate date") from exc
            if parsed.isoformat() != self.rate_date or not self.rate_source.strip():
                raise ValueError(f"{self.currency} accounting requires a rate date and source")
        object.__setattr__(self, "usd_to_currency_rate", rate)

    # -- where the ledger's record is, and its arithmetic -------------------

    @property
    def native(self) -> bool:
        """Whether the ledger's record is the native ``*_amount`` columns.

        False on a USD ledger, whose record stays the ``*_usd`` columns it has
        always written (the native ones beside them are kept by triggers and
        may be NULL on rows older than them).
        """
        return self.currency != USD

    @property
    def symbol(self) -> str:
        """How a message prefixes an amount in the ledger currency."""
        return SYMBOLS[self.currency]

    @property
    def quantum(self) -> Decimal:
        """What one counter or reservation increment is rounded to."""
        return NATIVE_QUANTUM if self.native else USD_QUANTUM

    def quantize(self, value: Any) -> Decimal:
        """*value* rounded to :attr:`quantum`, half away from zero.

        A float is taken at its exact binary value, as PostgreSQL's ``numeric``
        input takes what asyncpg sends for one; a decimal or a string exactly.
        """
        if isinstance(value, Decimal):
            exact = value
        elif isinstance(value, float):
            exact = Decimal(value)
        else:
            exact = Decimal(str(value))
        return exact.quantize(self.quantum, rounding=ROUND_HALF_UP)

    def number(self, value: Any) -> Money:
        """*value* in the ledger's arithmetic: a float on USD, a decimal otherwise.

        A float becomes the decimal of its shortest representation, the
        number it was written as; a decimal stays exact.
        """
        if not self.native:
            return float(value)
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @property
    def zero(self) -> Money:
        """Nothing, in the ledger's arithmetic."""
        return self.number(0)

    # -- conversions ---------------------------------------------------------

    def in_ledger(self, usd_value: Any) -> Money:
        """A USD value (a constant, a USD tariff) in the ledger: identity on USD."""
        if not self.native:
            return float(usd_value)
        return money_decimal(usd_value) * self.usd_to_currency_rate

    def as_usd(self, amount: Any) -> Any:
        """A ledger amount's USD equivalent for a USD contract: identity on USD."""
        if not self.native:
            return amount
        return money_decimal(amount) / self.usd_to_currency_rate

    def usd_value(self, amount: Any) -> float:
        """A ledger amount's USD equivalent as a float, for a float USD contract.

        Exactly the amount on a USD ledger (a float stays the same float).
        """
        return float(self.as_usd(amount))

    def from_usd(self, value: Any) -> Decimal:
        """Convert a true USD value into the deployment accounting unit, exactly."""
        return money_decimal(value) * self.usd_to_currency_rate

    def to_usd(self, value: Any) -> Decimal:
        """Return the USD equivalent for compatibility readers, exactly."""
        return money_decimal(value) / self.usd_to_currency_rate

    def accepts_tariff(self, currency: str) -> bool:
        """Whether a model tariff quoted in *currency* can be charged in this ledger.

        A tariff in the ledger currency is charged as quoted; a USD tariff is
        converted at the fixed rate. Any other quote has no declared rate.
        """
        return currency in (self.currency, USD)

    def logged(
        self, amount: Any, *, usd_key: str = "cost_usd", amount_key: str = "cost_amount"
    ) -> dict[str, str]:
        """How a log record or a report states a ledger *amount*.

        A USD ledger states it as it always has (*usd_key* only); another as
        its USD equivalent, its native amount and the currency.
        """
        if not self.native:
            return {usd_key: str(amount)}
        return {
            usd_key: str(self.to_usd(amount)),
            amount_key: str(amount),
            "currency": self.currency,
        }

    def label(self, amount: Any) -> str:
        """An amount for a human-readable message, to the cent."""
        return f"{self.symbol}{self.number(amount):.2f}"

    # -- persistence ---------------------------------------------------------

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
#: Whether a policy read from the database is bound (the gateway, after startup).
_bound = False


def get_accounting_policy() -> AccountingPolicy:
    """Return the policy bound at application startup."""
    return _policy


def bind_accounting_policy(policy: AccountingPolicy | None) -> None:
    """Bind the validated deployment policy, or reset to the USD default."""
    global _policy, _bound
    if policy is not None and not isinstance(policy, AccountingPolicy):
        raise TypeError("expected AccountingPolicy")
    _policy = policy or AccountingPolicy()
    _bound = policy is not None


def declared_currency_problem(currency: str) -> str | None:
    """Why a site configuration declaring *currency* cannot be saved in this process, if not.

    ``SITE_CONFIGURATION``'s ``accounting.currency`` (``USD`` when absent) is
    the currency the deployment expects its ledger to keep; startup refuses a
    database whose persisted policy differs. Once a policy is bound, a change
    that no longer matches it is refused before it is saved: the ledger's
    currency changes only through the offline migration, never by editing the
    declaration. A process with no bound policy (an offline tool) leaves the
    comparison to the next startup.
    """
    if not _bound or currency == _policy.currency:
        return None
    return (
        f"/accounting/currency must be {_policy.currency}, the currency of this "
        "deployment's ledger; changing it needs the offline currency migration"
    )


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
    if policy.native:
        marker = await store.get_setting(MIGRATION_KEY)
        if marker is None or marker.get("value_type") != "json":
            raise ValueError(f"{policy.currency} accounting requires a completed offline migration")
        marker_raw = marker["value"]
        completed = AccountingPolicy.from_dict(
            json.loads(marker_raw) if isinstance(marker_raw, str) else marker_raw
        )
        if completed != policy:
            raise ValueError("Accounting migration does not match the persisted policy")
    return policy
