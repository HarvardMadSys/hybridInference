"""The monthly limit of an organization: the last level a request is checked against.

After the API key, its project and the account comes the organization the
account is a member of. Its spend is one counter all of its members add to, so
a gate counts, on top of what it read, what every member has in flight and not
yet written in this process -- not only the caller.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from serving import accounting, quota
from serving.accounting import AccountingPolicy
from serving.spend_ledger import SpendCheck, SpendLedger

if TYPE_CHECKING:
    from serving.storage.base import CostIncrement

pytestmark = pytest.mark.asyncio

RATE = Decimal("7")


@pytest.fixture
def cny(monkeypatch: pytest.MonkeyPatch) -> AccountingPolicy:
    policy = AccountingPolicy(
        currency="CNY",
        usd_to_currency_rate=RATE,
        rate_date="2026-11-05",
        rate_source="https://example.test/synthetic-fixed-rate",
    )
    monkeypatch.setattr(accounting, "_policy", policy)
    return policy


class _AtomicStore:
    """A store that applies batches atomically, and waits at a gate when given one."""

    atomic_cost_increments = True

    def __init__(self) -> None:
        self.batches: list[list[CostIncrement]] = []
        self.gate: asyncio.Event | None = None

    async def increment_user_costs(self, increments: list[CostIncrement]) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.batches.append(list(increments))


def _check(ledger: SpendLedger | None, user_id: str = "u1", **overrides: Any) -> SpendCheck:
    fields: dict[str, Any] = {
        "ledger": ledger,
        "user_id": user_id,
        "key_id": None,
        "read_at": time.monotonic(),
        "account_quota_usd": 1000.0,
        "account_spent_usd": 0.0,
    }
    fields.update(overrides)
    return SpendCheck(**fields)


def _member(ledger: SpendLedger | None, user_id: str, limit: float | None, **overrides: Any):
    return _check(
        ledger,
        user_id,
        organization_id="o-1",
        organization_name="Example University",
        organization_limit_usd=limit,
        **overrides,
    )


# -- which limit ran out -------------------------------------------------------------


async def test_a_request_under_the_organizations_limit_is_not_refused():
    assert (
        quota.first_refusal(
            account_quota_usd=1000,
            account_spent_usd=0,
            organization_limit_usd=100,
            organization_spent_month_usd=99.98,
            organization_name="Example University",
        )
        is None
    )


async def test_the_organization_refuses_when_its_month_is_spent():
    refusal = quota.first_refusal(
        account_quota_usd=1000,
        account_spent_usd=0,
        organization_limit_usd=100,
        organization_spent_month_usd=99.995,
        organization_name="Example University",
    )

    assert (refusal.scope, refusal.window) == ("organization", "month")
    assert refusal.scope_name == "Example University"
    assert (refusal.limit_usd, refusal.spent_usd) == (100.0, 99.995)


async def test_an_organization_without_a_limit_refuses_nothing():
    assert (
        quota.first_refusal(
            account_quota_usd=1000,
            account_spent_usd=0,
            organization_limit_usd=None,
            organization_spent_month_usd=1e9,
        )
        is None
    )


async def test_the_members_own_limit_is_reported_before_the_organizations():
    refusal = quota.first_refusal(
        account_quota_usd=1000,
        account_spent_usd=0,
        account_quota_monthly_usd=20,
        account_spent_month_usd=20,
        organization_limit_usd=100,
        organization_spent_month_usd=100,
    )

    # The narrowest limit that ran out: the member's, though both did.
    assert (refusal.scope, refusal.window) == ("account", "month")


async def test_the_refusal_names_the_organization_and_when_its_month_starts_again():
    refusal = quota.BudgetRefusal(
        "organization", "month", 1000.0, 1000.02, scope_name="Example University"
    )

    body, headers = quota.refusal_payload(refusal, contact_email="support@example.edu")

    assert body["error"] == "Organization monthly limit exceeded"
    assert (body["code"], body["scope"], body["window"]) == (
        "budget_exceeded",
        "organization",
        "month",
    )
    assert body["organization"] == "Example University"
    assert (body["quota_usd"], body["spent_usd"], body["remaining_usd"]) == (1000.0, 1000.02, 0)
    assert "Example University" in body["message"]
    assert "administrator of your organization" in body["message"]
    assert body["reset_at"].endswith("-01T00:00:00+00:00")
    assert int(headers["Retry-After"]) == body["retry_after"] > 0
    # A key and a project are not named: neither is what ran out.
    assert "key_prefix" not in body
    assert "project" not in body


# -- what a lookup row carries --------------------------------------------------------


async def test_a_row_of_a_member_gives_the_organization_to_the_check():
    row = {
        "organization_id": "o-1",
        "organization_name": "Example University",
        "organization_limit_usd": Decimal("1000.0000"),
        "organization_limit_amount": Decimal("1000"),
        "organization_spent_month_usd": Decimal("12.500000"),
        "organization_spent_month_amount": Decimal("12.5"),
    }

    assert quota.organization_fields(row, native=False) == {
        "organization_id": "o-1",
        "organization_name": "Example University",
        "organization_limit_usd": 1000.0,
        "organization_spent_month_usd": 12.5,
    }


async def test_a_row_of_no_organization_gives_nothing():
    assert quota.organization_fields({"organization_id": None}, native=False) == {}
    # A lookup that reads no organization at all.
    assert quota.organization_fields({"user_id": "u1"}, native=False) == {}


async def test_an_organization_without_a_limit_is_still_named():
    fields = quota.organization_fields(
        {"organization_id": "o-1", "organization_name": "Example", "organization_limit_usd": None},
        native=False,
    )

    assert fields["organization_limit_usd"] is None
    assert fields["organization_spent_month_usd"] == 0.0


async def test_a_native_ledger_reads_the_native_limit_and_counter(cny):
    row = {
        "organization_id": "o-1",
        "organization_name": "Example University",
        "organization_limit_usd": Decimal("100"),
        "organization_limit_amount": Decimal("699.5"),
        "organization_spent_month_usd": Decimal("1"),
        "organization_spent_month_amount": Decimal("7.000000000001"),
    }

    fields = quota.organization_fields(row, native=True)

    # The exact native amounts, not the USD equivalents at the rate.
    assert fields["organization_limit_usd"] == Decimal("699.5")
    assert fields["organization_spent_month_usd"] == Decimal("7.000000000001")


async def test_a_native_limit_stored_before_its_native_column_is_read_at_the_rate(cny):
    fields = quota.organization_fields(
        {
            "organization_id": "o-1",
            "organization_limit_usd": Decimal("100"),
            "organization_limit_amount": None,
        },
        native=True,
    )

    assert fields["organization_limit_usd"] == Decimal("700")


# -- what the members have in flight ---------------------------------------------------


async def test_a_check_with_no_ledger_applies_the_limit_to_what_it_read():
    spent = _member(None, "u1", 1.0, organization_spent_month_usd=1.0)
    fits = _member(None, "u1", 1.0, organization_spent_month_usd=0.5)

    assert spent.refusal().scope == "organization"
    assert fits.refusal() is None


async def test_the_requests_of_every_member_in_flight_count_against_the_organization():
    ledger = SpendLedger(object())

    first, refusal = _member(ledger, "u1", 0.015).admit()
    assert first is not None and refusal is None
    # Another member: nothing of its own in flight, but its organization's
    # limit has no room left for a second request.
    _, refused = _member(ledger, "u2", 0.015).admit()

    assert (refused.scope, refused.window) == ("organization", "month")
    assert refused.scope_name == "Example University"
    # A member of another organization, and an account in none, are not held to it.
    assert (
        _check(ledger, "u3", organization_id="o-2", organization_limit_usd=0.015).refusal() is None
    )
    assert _check(ledger, "u4").refusal() is None

    first.release()
    assert _member(ledger, "u2", 0.015).refusal() is None


async def test_a_members_cost_not_yet_written_counts_against_the_organization():
    store = _AtomicStore()
    store.gate = asyncio.Event()
    ledger = SpendLedger(store)
    # A gate has seen the member, as it has before any of its costs.
    assert _member(ledger, "u1", 0.025).refusal() is None
    ledger.record("u1", 0.02)

    waiting = _member(ledger, "u2", 0.025)

    # 0.02 recorded and not written, and 0.01 for this request: over 0.025.
    assert waiting.refusal().scope == "organization"
    assert _check(ledger, "u4").refusal() is None

    store.gate.set()
    assert await ledger.flush()
    # A gate that read the counter before the write landed still counts it...
    assert waiting.refusal().scope == "organization"
    # ...and one that reads it afterwards finds it in the counter, once.
    await asyncio.sleep(0)
    later = _member(
        ledger,
        "u2",
        0.035,
        organization_spent_month_usd=0.02,
        read_at=time.monotonic() + 1,
    )
    assert later.refusal() is None


def _fields(check: SpendCheck) -> dict[str, Any]:
    return {name: getattr(check, name) for name in check.__dataclass_fields__}


async def test_a_member_never_seen_here_adds_nothing():
    ledger = SpendLedger(_AtomicStore())
    # u9 is charged in this process without any gate having named its
    # organization: nothing ties the cost to one.
    ledger.record("u9", 0.5)

    assert _member(ledger, "u1", 0.015).refusal() is None
    assert await ledger.flush()


async def test_what_the_organization_has_left_caps_a_large_reservation():
    ledger = SpendLedger(object())

    reservation, refusal = ledger.admit(
        _member(ledger, "u1", 100.0, organization_spent_month_usd=99.97), amount=1.0
    )

    assert refusal is None
    # Not the dollar it asked for: what the organization has left.
    assert reservation.amount == pytest.approx(0.03)
    assert _member(ledger, "u2", 100.0, organization_spent_month_usd=99.97).refusal().scope == (
        "organization"
    )
    reservation.release()


async def test_an_organization_without_a_limit_reserves_as_any_account_does():
    ledger = SpendLedger(object())

    reservation, refusal = ledger.admit(_member(ledger, "u1", None), amount=0.5)

    assert refusal is None
    assert reservation.amount == pytest.approx(0.5)
    assert _member(ledger, "u2", None).refusal() is None
    reservation.release()


async def test_the_limit_is_compared_exactly_on_a_native_ledger(cny):
    ledger = SpendLedger(object())
    estimate = quota.estimated_request_cost()
    limit = Decimal("7") + estimate

    fits = _check(
        ledger,
        "u1",
        account_quota_usd=Decimal("7000"),
        account_spent_usd=Decimal(0),
        organization_id="o-1",
        organization_name="Example University",
        organization_limit_usd=limit,
        organization_spent_month_usd=Decimal("7"),
    )
    over = SpendCheck(
        **{**_fields(fits), "organization_spent_month_usd": Decimal("7.000000000001")}
    )

    assert fits.refusal() is None
    refusal = over.refusal()
    assert refusal.scope == "organization"
    # The native amounts it compared, beside their USD equivalents.
    assert refusal.limit_amount == limit
    assert refusal.spent_amount == Decimal("7.000000000001")
