"""The account's monthly cost quota (``quota_monthly_cost_usd``) on the API-key door.

Admins could set it and the console showed what was left of it, but no request
was ever refused for it. NULL stays "no monthly limit"; a set one refuses like
the daily quota, after it, and renews at the start of the UTC month.
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


def _monthly(**overrides) -> dict:
    """A row of an account with a $30 monthly quota, as the merged lookup returns it."""
    return _row(**{"quota_monthly_cost_usd": 30.0, "account_spent_month_usd": 0.0, **overrides})


async def test_a_spent_monthly_quota_refuses_until_next_month():
    store = _store(_monthly(account_spent_month_usd=30.0, cost_usd=1.0))
    store.get_user_cost_period = AsyncMock()

    exc = await _refusal(store)

    body = exc.detail
    assert body["error"] == "Monthly cost quota exceeded"
    assert (body["code"], body["scope"], body["window"]) == ("budget_exceeded", "account", "month")
    assert (body["quota_usd"], body["spent_usd"], body["remaining_usd"]) == (30.0, 30.0, 0)
    assert body["reset_at"] == quota.next_utc_month_start().isoformat()
    assert "key_prefix" not in body
    assert exc.headers["X-RateLimit-Limit-Cost"] == "30.0"
    # The month's spend came with the auth lookup: no second read.
    store.get_user_cost_period.assert_not_awaited()


async def test_a_monthly_quota_with_room_left_lets_the_request_through():
    ctx = await _verify(_store(_monthly(account_spent_month_usd=29.98)))

    assert ctx["user_id"] == "owner-1"


async def test_no_monthly_quota_is_no_monthly_limit():
    """NULL is what every account had in effect before; it must not start refusing."""
    ctx = await _verify(_store(_row(quota_monthly_cost_usd=None, account_spent_month_usd=1e6)))

    assert ctx["user_id"] == "owner-1"


async def test_the_daily_quota_is_reported_before_the_monthly_one():
    exc = await _refusal(_store(_monthly(account_spent_month_usd=30.0, cost_usd=100.0)))

    assert (exc.detail["scope"], exc.detail["window"]) == ("account", "day")


async def test_a_spent_key_budget_is_reported_before_the_monthly_quota():
    store = _store(
        _monthly(account_spent_month_usd=30.0, budget_daily_usd=1, key_spent_today_usd=1)
    )

    exc = await _refusal(store)

    assert exc.detail["scope"] == "key"


async def test_a_zero_monthly_quota_refuses_everything():
    exc = await _refusal(_store(_row(quota_monthly_cost_usd=0, account_spent_month_usd=0)))

    assert exc.detail["window"] == "month"


async def test_a_path_without_the_merged_spend_reads_the_months_counter():
    """A store without the merged lookup (a test double, older stores) still gates the month."""
    store = MagicMock()
    row = _monthly()
    del row["account_spent_month_usd"]
    store.get_auth_context_by_key_hash = AsyncMock(return_value=row)
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    store.get_user_cost_period = AsyncMock(return_value=30.0)
    store.update_key_last_used = AsyncMock()

    exc = await _refusal(store)

    assert exc.detail["window"] == "month"
    store.get_user_cost_period.assert_awaited_once_with("owner-1", "month")


async def test_stored_work_stops_at_the_monthly_quota():
    store = _store(_monthly(account_spent_month_usd=30.0))

    with pytest.raises(StoredKeyQuotaExceeded) as caught:
        await authenticate_stored_key(hash_api_key(KEY), store)

    assert (caught.value.scope, caught.value.window) == ("account", "month")
    assert caught.value.reset_at == quota.next_utc_month_start()
