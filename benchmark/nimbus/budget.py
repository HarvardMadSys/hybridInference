"""Durable, fail-closed CNY accounting for Nimbus's paid HTTP attempts.

All experiment processes must share one ledger on a local filesystem. Reserve
before every attempt (including retries), then mark it dispatched immediately
before HTTP I/O. Only authoritative final usage can settle an attempt. A crash,
timeout, missing usage, or HTTP error never releases its reservation.
Settled costs price authoritative usage at configured rates; they are estimates,
not statements of the provider's invoice or the account's actual debit.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterator

_NANOYUAN = 1_000_000_000
_MAX_TOKENS = (1 << 63) - 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_OUTSTANDING = {"reserved", "dispatched", "unknown"}
_CNY_RATE_FIELDS = (
    "input_cny_per_million",
    "cached_input_cny_per_million",
    "output_cny_per_million",
)
_BILLING_RATE_FIELDS = ("input_per_million", "cached_input_per_million", "output_per_million")
_COST_BASIS = "configured_rate_estimate_not_invoice"


class BudgetError(RuntimeError):
    """The spending ledger refused an operation."""


class BudgetExceeded(BudgetError):
    """The campaign has no permitted budget for this new attempt."""


class AttemptConflict(BudgetError):
    """An attempt ID was reused with different terms or an unsafe transition."""


class BudgetConfigurationError(BudgetError, ValueError):
    """The persisted campaign or cap differs from the requested configuration."""


def _amount(value: Decimal | str | int, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ValueError(f"{name} must be an exact Decimal, decimal string, or integer")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite nonnegative decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite nonnegative decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _money(nanoyuan: int) -> str:
    sign = "-" if nanoyuan < 0 else ""
    whole, fractional = divmod(abs(nanoyuan), _NANOYUAN)
    return f"{sign}{whole}.{fractional:09d}"


def _tokens(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_TOKENS:
        raise ValueError(f"{name} must be a nonnegative 64-bit integer")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a short identifier using letters, digits, . _ : or -")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class PricingProvenance:
    """Original billing rates and the explicitly chosen CNY accounting conversion.

    For example, USD peak rates converted with a conservative budget exchange
    rate remain labelled USD peak estimates, not official CNY prices.
    """

    billing_currency: str
    input_per_million: Decimal | str | int
    cached_input_per_million: Decimal | str | int
    output_per_million: Decimal | str | int
    cny_per_billing_unit: Decimal | str | int
    price_basis: str
    conversion_basis: str
    source_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.billing_currency, str) or not re.fullmatch(
            r"[A-Z]{3}", self.billing_currency
        ):
            raise ValueError("billing_currency must be a three-letter uppercase currency code")
        for name in (*_BILLING_RATE_FIELDS, "cny_per_billing_unit"):
            object.__setattr__(self, name, _amount(getattr(self, name), name))
        if self.cny_per_billing_unit <= 0:
            raise ValueError("cny_per_billing_unit must be positive")
        if self.billing_currency == "CNY" and self.cny_per_billing_unit != 1:
            raise ValueError("CNY billing requires a conversion of exactly one")
        _identifier(self.price_basis, "price_basis")
        _identifier(self.conversion_basis, "conversion_basis")
        if self.source_url is not None:
            if not isinstance(self.source_url, str) or len(self.source_url) > 2048:
                raise ValueError("source_url must be a public HTTPS pricing URL")
            parsed = urlsplit(self.source_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(character.isspace() for character in self.source_url)
            ):
                raise ValueError("source_url must be HTTPS without credentials, query or fragment")

    def to_dict(self) -> dict:
        """Return exact original rates and conversion provenance as JSON-safe values."""
        return {
            "billing_currency": self.billing_currency,
            **{
                name: _decimal_text(getattr(self, name))
                for name in (*_BILLING_RATE_FIELDS, "cny_per_billing_unit")
            },
            "price_basis": self.price_basis,
            "conversion_basis": self.conversion_basis,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class TokenPrices:
    """Explicit CNY accounting rates per million tokens, optionally with provenance."""

    input_cny_per_million: Decimal | str | int
    cached_input_cny_per_million: Decimal | str | int
    output_cny_per_million: Decimal | str | int
    provenance: PricingProvenance | dict | None = None

    def __post_init__(self) -> None:
        for name in _CNY_RATE_FIELDS:
            object.__setattr__(self, name, _amount(getattr(self, name), name))
        if isinstance(self.provenance, dict):
            object.__setattr__(self, "provenance", PricingProvenance(**self.provenance))
        if self.provenance is not None:
            if not isinstance(self.provenance, PricingProvenance):
                raise ValueError("provenance must be PricingProvenance or its JSON dictionary")
            for cny_field, billing_field in zip(
                _CNY_RATE_FIELDS, _BILLING_RATE_FIELDS, strict=True
            ):
                expected = Fraction(getattr(self.provenance, billing_field)) * Fraction(
                    self.provenance.cny_per_billing_unit
                )
                if Fraction(getattr(self, cny_field)) != expected:
                    raise ValueError(
                        f"{cny_field} does not match original billing rate and conversion"
                    )

    def to_dict(self) -> dict:
        """Return exact accounting rates and optional provenance for persistent records."""
        result = {name: _decimal_text(getattr(self, name)) for name in _CNY_RATE_FIELDS}
        if self.provenance is not None:
            result["provenance"] = self.provenance.to_dict()
        return result

    def _quote_nanos(self, input_tokens: int, output_tokens: int, cached_input_tokens: int) -> int:
        _tokens(input_tokens, "input_tokens")
        _tokens(output_tokens, "output_tokens")
        _tokens(cached_input_tokens, "cached_input_tokens")
        if cached_input_tokens > input_tokens:
            raise ValueError("cached_input_tokens cannot exceed total input_tokens")
        total = (
            Fraction(self.input_cny_per_million) * (input_tokens - cached_input_tokens)
            + Fraction(self.cached_input_cny_per_million) * cached_input_tokens
            + Fraction(self.output_cny_per_million) * output_tokens
        ) * 1000
        return -(-total.numerator // total.denominator)

    def quote(
        self, *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> Decimal:
        """Price known usage, rounding total liability up to the next nanoyuan."""
        return Decimal(_money(self._quote_nanos(input_tokens, output_tokens, cached_input_tokens)))

    def quote_billing_currency(
        self, *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> Decimal:
        """Estimate cost at original configured rates, in the original currency."""
        if self.provenance is None:
            return self.quote(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
            )
        original_rates = TokenPrices(
            self.provenance.input_per_million,
            self.provenance.cached_input_per_million,
            self.provenance.output_per_million,
        )
        return original_rates.quote(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
        )

    def reserve_quote(self, *, input_tokens_upper_bound: int, max_output_tokens: int) -> Decimal:
        """Reserve worst-case input/cache pricing and the entire output allowance."""
        worst_case = TokenPrices(
            max(self.input_cny_per_million, self.cached_input_cny_per_million),
            max(self.input_cny_per_million, self.cached_input_cny_per_million),
            self.output_cny_per_million,
        )
        return worst_case.quote(
            input_tokens=input_tokens_upper_bound, output_tokens=max_output_tokens
        )


class BudgetLedger:
    """One immutable campaign cap with serialized, durable attempt accounting.

    A dispatch marker is deliberately not retryable: retrying the same HTTP
    request needs a fresh attempt ID and reservation. This avoids double spend
    after a process crashes between marking dispatch and receiving a response.
    A known charge above its reservation is always recorded and permanently
    blocks new spending, even when the campaign cap has not yet been exceeded.
    """

    def __init__(self, path: str | Path, campaign_id: str, cap_cny: Decimal | str | int):
        self.campaign_id = _identifier(campaign_id, "campaign_id")
        requested_cap = _amount(cap_cny, "cap_cny")
        cap_fraction = Fraction(requested_cap) * _NANOYUAN
        cap_nanos = cap_fraction.numerator // cap_fraction.denominator
        if cap_nanos <= 0:
            raise BudgetConfigurationError("cap_cny must be at least one nanoyuan")
        if str(path) == ":memory:":
            raise BudgetConfigurationError("a persistent on-disk ledger path is required")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS nimbus_campaign ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "campaign_id TEXT NOT NULL, requested_cap TEXT NOT NULL, "
                "cap_nanos TEXT NOT NULL, blocked INTEGER NOT NULL DEFAULT 0, "
                "blocked_reason TEXT, created_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS nimbus_attempts ("
                "attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, terms TEXT NOT NULL, "
                "status TEXT NOT NULL, reserved_nanos TEXT NOT NULL, actual_nanos TEXT, "
                "usage TEXT, created_at TEXT NOT NULL, dispatched_at TEXT, "
                "settled_at TEXT, cancelled_at TEXT, unknown_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS nimbus_events ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL, "
                "event TEXT NOT NULL, timestamp TEXT NOT NULL)"
            )
            existing = conn.execute("SELECT * FROM nimbus_campaign WHERE singleton = 1").fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO nimbus_campaign "
                    "(singleton, campaign_id, requested_cap, cap_nanos, created_at) "
                    "VALUES (1, ?, ?, ?, ?)",
                    (self.campaign_id, _decimal_text(requested_cap), str(cap_nanos), _now()),
                )
            elif (
                existing["campaign_id"] != self.campaign_id
                or Decimal(existing["requested_cap"]) != requested_cap
            ):
                raise BudgetConfigurationError("the existing ledger campaign and cap are immutable")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _get(conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        _identifier(attempt_id, "attempt_id")
        row = conn.execute(
            "SELECT * FROM nimbus_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise AttemptConflict(f"unknown attempt: {attempt_id}")
        return row

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        terms = json.loads(row["terms"])
        reserved = int(row["reserved_nanos"])
        actual = int(row["actual_nanos"]) if row["actual_nanos"] is not None else None
        usage = json.loads(row["usage"]) if row["usage"] is not None else None
        prices = TokenPrices(**terms["prices"])
        return {
            "attempt_id": row["attempt_id"],
            **terms,
            "status": row["status"],
            "reserved_cny": _money(reserved),
            "estimated_cost_cny": _money(actual) if actual is not None else None,
            "estimated_cost_billing_currency": (
                str(prices.quote_billing_currency(**usage)) if usage is not None else None
            ),
            "billing_currency": (
                prices.provenance.billing_currency if prices.provenance is not None else "CNY"
            ),
            "cost_basis": _COST_BASIS,
            "entry_kind": terms.get("entry_kind", "http_attempt"),
            "reservation_exceeded": actual is not None and actual > reserved,
            "usage": usage,
            **{
                name: row[name]
                for name in (
                    "created_at",
                    "dispatched_at",
                    "settled_at",
                    "cancelled_at",
                    "unknown_at",
                )
            },
        }

    @staticmethod
    def _totals(conn: sqlite3.Connection) -> tuple[int, int]:
        spent = reserved = 0
        for row in conn.execute("SELECT status, actual_nanos, reserved_nanos FROM nimbus_attempts"):
            if row["status"] == "settled":
                spent += int(row["actual_nanos"])
            elif row["status"] in _OUTSTANDING:
                reserved += int(row["reserved_nanos"])
        return spent, reserved

    @staticmethod
    def _event(conn: sqlite3.Connection, attempt_id: str, event: str, timestamp: str) -> None:
        conn.execute(
            "INSERT INTO nimbus_events (attempt_id, event, timestamp) VALUES (?, ?, ?)",
            (attempt_id, event, timestamp),
        )

    def reserve(
        self,
        *,
        attempt_id: str,
        run_id: str,
        prices: TokenPrices,
        input_tokens_upper_bound: int,
        max_output_tokens: int,
    ) -> dict:
        """Atomically reserve before HTTP I/O; identical IDs return the prior record."""
        _identifier(attempt_id, "attempt_id")
        _identifier(run_id, "run_id")
        if not isinstance(prices, TokenPrices):
            raise ValueError("prices must be TokenPrices")
        reserved = int(
            Fraction(
                prices.reserve_quote(
                    input_tokens_upper_bound=input_tokens_upper_bound,
                    max_output_tokens=max_output_tokens,
                )
            )
            * _NANOYUAN
        )
        terms = json.dumps(
            {
                "run_id": run_id,
                "prices": prices.to_dict(),
                "input_tokens_upper_bound": input_tokens_upper_bound,
                "max_output_tokens": max_output_tokens,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM nimbus_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing is not None:
                if existing["terms"] != terms:
                    raise AttemptConflict(
                        "attempt ID already exists with different reservation terms"
                    )
                return {**self._record(existing), "created": False}
            campaign = conn.execute("SELECT * FROM nimbus_campaign WHERE singleton = 1").fetchone()
            spent, outstanding = self._totals(conn)
            if campaign["blocked"]:
                raise BudgetExceeded("spending is blocked by a prior accounting overrun")
            if spent + outstanding + reserved > int(campaign["cap_nanos"]):
                raise BudgetExceeded("this attempt would exceed the cumulative campaign cap")
            timestamp = _now()
            conn.execute(
                "INSERT INTO nimbus_attempts "
                "(attempt_id, run_id, terms, status, reserved_nanos, created_at) "
                "VALUES (?, ?, ?, 'reserved', ?, ?)",
                (attempt_id, run_id, terms, str(reserved), timestamp),
            )
            self._event(conn, attempt_id, "reserved", timestamp)
            return {**self._record(self._get(conn, attempt_id)), "created": True}

    def mark_dispatched(self, attempt_id: str) -> dict:
        """Claim exactly one HTTP dispatch; repeated claims are refused."""
        with self._transaction() as conn:
            row = self._get(conn, attempt_id)
            if row["status"] != "reserved":
                raise AttemptConflict("only a new reserved attempt can be dispatched")
            campaign = conn.execute(
                "SELECT blocked FROM nimbus_campaign WHERE singleton = 1"
            ).fetchone()
            if campaign["blocked"]:
                raise BudgetExceeded("spending is blocked by a prior accounting overrun")
            timestamp = _now()
            conn.execute(
                "UPDATE nimbus_attempts SET status = 'dispatched', dispatched_at = ? "
                "WHERE attempt_id = ?",
                (timestamp, attempt_id),
            )
            self._event(conn, attempt_id, "dispatched", timestamp)
            return self._record(self._get(conn, attempt_id))

    def settle(
        self,
        attempt_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> dict:
        """Price authoritative final usage, including any estimate above the cap.

        input_tokens includes cached tokens. If cache usage is absent, zero is
        conservative only when cached tokens cost no more than uncached tokens.
        Never use this operation for incomplete stream usage or assumed zero use.
        The resulting estimated_cost_cny is not an invoice or an observed debit.
        """
        usage = json.dumps(
            {
                "input_tokens": _tokens(input_tokens, "input_tokens"),
                "output_tokens": _tokens(output_tokens, "output_tokens"),
                "cached_input_tokens": _tokens(cached_input_tokens, "cached_input_tokens"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if cached_input_tokens > input_tokens:
            raise ValueError("cached_input_tokens cannot exceed total input_tokens")
        with self._transaction() as conn:
            row = self._get(conn, attempt_id)
            if row["status"] == "settled":
                if row["usage"] != usage:
                    raise AttemptConflict("attempt already settled with different usage")
                return self._record(row)
            if row["status"] not in {"dispatched", "unknown"}:
                raise AttemptConflict("only dispatched or unknown attempts can be settled")
            prices = TokenPrices(**json.loads(row["terms"])["prices"])
            actual = prices._quote_nanos(input_tokens, output_tokens, cached_input_tokens)
            timestamp = _now()
            conn.execute(
                "UPDATE nimbus_attempts SET status = 'settled', actual_nanos = ?, "
                "usage = ?, settled_at = ? WHERE attempt_id = ?",
                (str(actual), usage, timestamp, attempt_id),
            )
            if actual > int(row["reserved_nanos"]):
                conn.execute(
                    "UPDATE nimbus_campaign SET blocked = 1, "
                    "blocked_reason = 'settlement_exceeded_reservation' WHERE singleton = 1"
                )
            self._event(conn, attempt_id, "settled", timestamp)
            return self._record(self._get(conn, attempt_id))

    def import_prior_usage(
        self,
        *,
        attempt_id: str,
        run_id: str,
        prices: TokenPrices,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
        evidence_id: str,
    ) -> dict:
        """Atomically account for a known paid call made before this ledger existed.

        This performs no HTTP dispatch. The audit records the import time and an
        evidence identifier, not an invented historical request timestamp. Unlike
        a new reservation, already incurred usage is recorded even above the cap;
        any overrun blocks subsequent spending. Reimports must match exactly.
        """
        _identifier(attempt_id, "attempt_id")
        _identifier(run_id, "run_id")
        _identifier(evidence_id, "evidence_id")
        if not isinstance(prices, TokenPrices):
            raise ValueError("prices must be TokenPrices")
        estimate = prices._quote_nanos(input_tokens, output_tokens, cached_input_tokens)
        terms = json.dumps(
            {
                "run_id": run_id,
                "prices": prices.to_dict(),
                "input_tokens_upper_bound": input_tokens,
                "max_output_tokens": output_tokens,
                "entry_kind": "historical_usage",
                "evidence_id": evidence_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        usage = json.dumps(
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM nimbus_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing is not None:
                if existing["terms"] != terms or existing["usage"] != usage:
                    raise AttemptConflict(
                        "prior-usage ID already exists with different terms or usage"
                    )
                return {**self._record(existing), "created": False}
            timestamp = _now()
            conn.execute(
                "INSERT INTO nimbus_attempts "
                "(attempt_id, run_id, terms, status, reserved_nanos, actual_nanos, "
                "usage, created_at, settled_at) VALUES (?, ?, ?, 'settled', ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    run_id,
                    terms,
                    str(estimate),
                    str(estimate),
                    usage,
                    timestamp,
                    timestamp,
                ),
            )
            campaign = conn.execute(
                "SELECT cap_nanos FROM nimbus_campaign WHERE singleton = 1"
            ).fetchone()
            spent, outstanding = self._totals(conn)
            if spent + outstanding > int(campaign["cap_nanos"]):
                conn.execute(
                    "UPDATE nimbus_campaign SET blocked = 1, "
                    "blocked_reason = 'historical_usage_exceeded_cap' WHERE singleton = 1"
                )
            self._event(conn, attempt_id, "prior_usage_imported", timestamp)
            return {**self._record(self._get(conn, attempt_id)), "created": True}

    def mark_unknown(self, attempt_id: str) -> dict:
        """Retain the entire reservation when authoritative final usage is absent."""
        with self._transaction() as conn:
            row = self._get(conn, attempt_id)
            if row["status"] == "unknown":
                return self._record(row)
            if row["status"] not in {"reserved", "dispatched"}:
                raise AttemptConflict("a terminal attempt cannot become unknown")
            timestamp = _now()
            conn.execute(
                "UPDATE nimbus_attempts SET status = 'unknown', unknown_at = ? WHERE attempt_id = ?",
                (timestamp, attempt_id),
            )
            self._event(conn, attempt_id, "unknown", timestamp)
            return self._record(self._get(conn, attempt_id))

    def cancel_before_dispatch(self, attempt_id: str) -> dict:
        """Release only a reservation proven never to have been dispatched."""
        with self._transaction() as conn:
            row = self._get(conn, attempt_id)
            if row["status"] == "cancelled":
                return self._record(row)
            if row["status"] != "reserved":
                raise AttemptConflict("a dispatched or uncertain attempt cannot be refunded")
            timestamp = _now()
            conn.execute(
                "UPDATE nimbus_attempts SET status = 'cancelled', cancelled_at = ? "
                "WHERE attempt_id = ?",
                (timestamp, attempt_id),
            )
            self._event(conn, attempt_id, "cancelled_before_dispatch", timestamp)
            return self._record(self._get(conn, attempt_id))

    def snapshot(self) -> dict:
        """Return JSON-safe totals; negative remaining budget is never hidden."""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM nimbus_campaign WHERE singleton = 1").fetchone()
            spent, reserved = self._totals(conn)
            cap = int(row["cap_nanos"])
            remaining = cap - spent - reserved
            return {
                "campaign_id": row["campaign_id"],
                "cap_cny": _money(cap),
                "estimated_spent_cny": _money(spent),
                "cost_basis": _COST_BASIS,
                "spent_cny": _money(spent),
                "outstanding_reserved_cny": _money(reserved),
                "committed_cny": _money(spent + reserved),
                "remaining_cny": _money(remaining),
                "available_cny": _money(0 if row["blocked"] else max(0, remaining)),
                "blocked": bool(row["blocked"]),
                "blocked_reason": row["blocked_reason"],
                "cap_exceeded": remaining < 0,
                "created_at": row["created_at"],
            }

    def export_entries(self, *, run_id: str | None = None) -> list[dict]:
        """Export IDs, prices, counts, timestamps and events; no prompts or keys."""
        if run_id is not None:
            _identifier(run_id, "run_id")
        with self._transaction() as conn:
            query = "SELECT * FROM nimbus_attempts"
            params = ()
            if run_id is not None:
                query += " WHERE run_id = ?"
                params = (run_id,)
            query += " ORDER BY created_at, attempt_id"
            records = []
            for row in conn.execute(query, params).fetchall():
                record = self._record(row)
                record["events"] = [
                    dict(event)
                    for event in conn.execute(
                        "SELECT sequence, event, timestamp FROM nimbus_events "
                        "WHERE attempt_id = ? ORDER BY sequence",
                        (row["attempt_id"],),
                    )
                ]
                records.append(record)
            return records
