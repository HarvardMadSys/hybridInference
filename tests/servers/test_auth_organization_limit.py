"""The monthly limit of an organization on the API-key door, and the doors beside it.

A member's request is checked against its key, its project and its account, as
any request is, and then against the organization the account is a member of:
its limit and its counter arrive with the auth lookup. An account in no
organization is never refused for one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving import quota
from serving.servers.auth import StoredKeyQuotaExceeded, authenticate_stored_key, hash_api_key

from .test_auth_key_limits import KEY, _refusal, _row, _store, _verify


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    monkeypatch.delenv("RAG_API_KEY", raising=False)


def _organization(**overrides) -> dict:
    """The columns a lookup carries of a member's organization: a $1,000 monthly limit."""
    columns = {
        "organization_id": "o-1",
        "organization_name": "Example University",
        "organization_limit_usd": 1000.0,
        "organization_limit_amount": 1000.0,
        "organization_spent_month_usd": 0.0,
        "organization_spent_month_amount": 0.0,
    }
    columns.update(overrides)
    return columns


def _member(**overrides) -> dict:
    """A row of a member, as the merged lookup returns it."""
    return _row(**{**_organization(), **overrides})


_END_USER = {
    "id": "end-user-1",
    "user_name": "End User",
    "role": "free",
    "status": "active",
    "preferences": None,
    "max_concurrent_requests": None,
}


async def test_a_spent_organization_limit_refuses_until_next_month():
    store = _store(_member(organization_spent_month_usd=1000.0, cost_usd=1.0))

    exc = await _refusal(store)

    body = exc.detail
    assert body["error"] == "Organization monthly limit exceeded"
    assert (body["code"], body["scope"], body["window"]) == (
        "budget_exceeded",
        "organization",
        "month",
    )
    assert body["organization"] == "Example University"
    assert (body["quota_usd"], body["spent_usd"], body["remaining_usd"]) == (1000.0, 1000.0, 0)
    assert body["reset_at"] == quota.next_utc_month_start().isoformat()
    assert "key_prefix" not in body
    assert exc.headers["X-RateLimit-Limit-Cost"] == "1000.0"
    # The organization came with the auth lookup: no second read.
    store.get_organization_spend_scope.assert_not_awaited()


async def test_an_organization_with_room_left_lets_the_request_through():
    ctx = await _verify(_store(_member(organization_spent_month_usd=999.98)))

    assert ctx["user_id"] == "owner-1"


async def test_an_organization_without_a_limit_refuses_nothing():
    ctx = await _verify(
        _store(_member(organization_limit_usd=None, organization_spent_month_usd=1e9))
    )

    assert ctx["user_id"] == "owner-1"


async def test_an_account_in_no_organization_is_never_refused_for_one():
    """NULL throughout is what the lookup returns for an account in no organization."""
    row = _row(**dict.fromkeys(_organization()))

    ctx = await _verify(_store(row))

    assert ctx["user_id"] == "owner-1"


async def test_the_members_own_limit_is_reported_before_the_organizations():
    store = _store(
        _member(
            quota_monthly_cost_usd=20.0,
            account_spent_month_usd=20.0,
            organization_spent_month_usd=1000.0,
        )
    )

    exc = await _refusal(store)

    assert (exc.detail["scope"], exc.detail["window"]) == ("account", "month")


async def test_a_zero_limit_refuses_every_member():
    exc = await _refusal(_store(_member(organization_limit_usd=0.0)))

    assert exc.detail["scope"] == "organization"


def _identity_only(row: dict) -> MagicMock:
    """A store that answers with the cached identity lookup: limits, and no spend."""
    for column in ("organization_spent_month_usd", "organization_spent_month_amount"):
        del row[column]
    store = MagicMock()
    store.get_auth_context_by_key_hash = AsyncMock(return_value=row)
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    store.get_organization_month_spend = AsyncMock(
        return_value={"cost_usd": 1000.0, "cost_amount": 1000.0}
    )
    store.update_key_last_used = AsyncMock()
    return store


async def test_an_identity_that_came_without_spend_reads_the_organizations_counter():
    """Taken as $0, the cached lookup would pass a limit that is spent."""
    store = _identity_only(_member())

    exc = await _refusal(store)

    assert (exc.detail["scope"], exc.detail["spent_usd"]) == ("organization", 1000.0)
    store.get_organization_month_spend.assert_awaited_once_with("o-1")


async def test_an_organization_without_a_limit_costs_no_read_of_its_counter():
    store = _identity_only(_member(organization_limit_usd=None, organization_limit_amount=None))

    ctx = await _verify(store)

    assert ctx["user_id"] == "owner-1"
    store.get_organization_month_spend.assert_not_awaited()


async def test_stored_work_stops_at_the_organizations_limit():
    store = _store(_member(organization_spent_month_usd=1000.0))

    with pytest.raises(StoredKeyQuotaExceeded) as caught:
        await authenticate_stored_key(hash_api_key(KEY), store)

    assert (caught.value.scope, caught.value.window) == ("organization", "month")
    assert caught.value.reset_at == quota.next_utc_month_start()


async def test_what_is_spent_on_behalf_of_a_member_counts_against_its_organization(monkeypatch):
    """The cost is the end user's, so it is the end user's organization that is checked."""
    monkeypatch.setenv("RAG_API_KEY", KEY)
    # The service account itself is in no organization.
    store = _store(_row())
    store.get_user_by_id = AsyncMock(return_value=_END_USER)
    store.get_quota_context_for_user = AsyncMock(
        return_value=[
            {
                "quota_daily_cost_usd": 10.0,
                "quota_monthly_cost_usd": None,
                **_organization(organization_spent_month_usd=1000.0),
            }
        ]
    )

    with pytest.raises(Exception) as caught:
        await _verify(store, x_on_behalf_of="end-user-1")

    assert caught.value.status_code == 429
    assert caught.value.detail["scope"] == "organization"
    # The organization came with the end user's quota row: no second read.
    store.get_organization_spend_scope.assert_not_awaited()


async def test_a_member_without_a_key_is_checked_against_its_organization_too(monkeypatch):
    """No key row answers for such an account, so its organization is read on its own."""
    monkeypatch.setenv("RAG_API_KEY", KEY)
    monkeypatch.setattr(
        "serving.servers.routers.user_routes.get_default_daily_quota_for_role",
        AsyncMock(return_value=10.0),
    )
    store = _store(_row())
    store.get_user_by_id = AsyncMock(return_value=_END_USER)
    store.get_quota_context_for_user = AsyncMock(return_value=[])
    store.get_user_cost_period = AsyncMock(return_value=0.0)
    store.get_organization_spend_scope = AsyncMock(
        return_value={
            **_organization(organization_spent_month_usd=1000.0),
            "quota_monthly_cost_usd": 20.0,
            "quota_monthly_cost_amount": 20.0,
        }
    )

    with pytest.raises(Exception) as caught:
        await _verify(store, x_on_behalf_of="end-user-1")

    assert caught.value.status_code == 429
    assert caught.value.detail["scope"] == "organization"
    store.get_organization_spend_scope.assert_awaited_once_with("end-user-1")


async def test_a_member_without_a_key_is_held_to_the_monthly_quota_its_organization_gave(
    monkeypatch,
):
    """Its own, or the default of the organization: the read gives the one that applies."""
    monkeypatch.setenv("RAG_API_KEY", KEY)
    monkeypatch.setattr(
        "serving.servers.routers.user_routes.get_default_daily_quota_for_role",
        AsyncMock(return_value=10.0),
    )
    store = _store(_row())
    store.get_user_by_id = AsyncMock(return_value=_END_USER)
    store.get_quota_context_for_user = AsyncMock(return_value=[])
    # The month's spend of the member reaches its monthly quota of $20.
    store.get_user_cost_period = AsyncMock(return_value=20.0)
    store.get_organization_spend_scope = AsyncMock(
        return_value={
            **_organization(),
            "quota_monthly_cost_usd": 20.0,
            "quota_monthly_cost_amount": 20.0,
        }
    )

    with pytest.raises(Exception) as caught:
        await _verify(store, x_on_behalf_of="end-user-1")

    assert caught.value.status_code == 429
    assert (caught.value.detail["scope"], caught.value.detail["window"]) == ("account", "month")
    assert caught.value.detail["quota_usd"] == 20.0
