"""A key's own budgets and model allowlist on the auth paths (P2a).

The key's limits apply exactly when the request is attributed to the key: a
direct request, or stored work under the key that created it. Refusals name
the narrowest limit that ran out, and keep every field of the account's 429.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from serving import quota
from serving.model_access import KEY_ALLOWED_MODELS_KEY
from serving.servers.auth import (
    StoredKeyQuotaExceeded,
    authenticate_stored_key,
    enforce_daily_quota,
    hash_api_key,
    optional_verify_api_key,
    verify_api_key,
    verify_batch_management_identity,
)
from serving.storage.base import OperationalStore

KEY = "hyi-limited-key"


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    monkeypatch.delenv("RAG_API_KEY", raising=False)


def _row(**overrides) -> dict:
    """An auth-context row as ``get_auth_context_with_spend`` returns it."""
    row = {
        "id": 21,
        "user_id": "owner-1",
        "user_name": "Owner",
        "quota_daily_cost_usd": 100.0,
        "role": "free",
        "email": "owner@example.com",
        "email_verified": True,
        "preferences": {},
        "max_concurrent_requests": None,
        "key_prefix": "hyi-limited1",
        "key_name": "ci",
        "budget_daily_usd": None,
        "budget_monthly_usd": None,
        "allowed_models": None,
        "key_spent_today_usd": 0,
        "key_spent_month_usd": 0,
        "cost_usd": 0.0,
    }
    row.update(overrides)
    return row


def _store(row: dict | None) -> MagicMock:
    """A spec'd store, so the merged single-round-trip lookup is the one used."""
    store = MagicMock(spec=OperationalStore)
    store.get_auth_context_with_spend = AsyncMock(return_value=row)
    store.get_auth_context_by_key_hash = AsyncMock(return_value=row)
    store.get_auth_context_lightweight = AsyncMock(return_value=row)
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    store.get_key_spend = AsyncMock(return_value=(0.0, 0.0))
    store.update_key_last_used = AsyncMock()
    return store


async def _verify(store, *, x_on_behalf_of: str | None = None) -> dict:
    # Every header explicitly: the dependency's defaults are ``Header`` markers.
    return await verify_api_key(
        request=MagicMock(spec=Request),
        authorization=f"Bearer {KEY}",
        x_api_key=None,
        x_on_behalf_of=x_on_behalf_of,
        op_store=store,
    )


async def _refusal(store) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await _verify(store)
    await asyncio.sleep(0)  # let the fire-and-forget rejection log run
    assert caught.value.status_code == 429
    return caught.value


async def test_a_key_under_its_budgets_passes_and_carries_its_allowlist():
    store = _store(
        _row(
            budget_daily_usd=5,
            budget_monthly_usd=50,
            key_spent_today_usd=4.98,
            key_spent_month_usd=49.98,
            allowed_models=["m-a", "m-b"],
        )
    )

    ctx = await _verify(store)

    assert ctx["api_key_id"] == 21
    assert ctx[KEY_ALLOWED_MODELS_KEY] == ["m-a", "m-b"]
    store.get_key_spend.assert_not_awaited()


async def test_a_key_without_an_allowlist_carries_none():
    ctx = await _verify(_store(_row()))

    assert KEY_ALLOWED_MODELS_KEY not in ctx


async def test_the_daily_budget_refuses_with_a_body_that_names_the_key(monkeypatch):
    monkeypatch.setattr(
        quota, "next_utc_midnight", lambda now=None: datetime(2026, 11, 4, tzinfo=UTC)
    )
    store = _store(_row(budget_daily_usd=5, key_spent_today_usd=4.995))

    exc = await _refusal(store)

    body = exc.detail
    assert body["error"] == "API key daily budget exceeded"
    assert (body["code"], body["scope"], body["window"]) == ("budget_exceeded", "key", "day")
    assert body["key_prefix"] == "hyi-limited1"
    assert body["quota_usd"] == 5.0 and body["spent_usd"] == 4.995
    assert body["remaining_usd"] == pytest.approx(0.005)
    assert body["reset_at"] == "2026-11-04T00:00:00+00:00"
    assert '"ci"' in body["message"] and "$5.00" in body["message"]
    # The account's fields are all still there.
    account_body, _ = quota.refusal_payload(quota.BudgetRefusal("account", "day", 1.0, 0.0))
    assert set(account_body) <= set(body)
    assert exc.headers["X-RateLimit-Limit-Cost"] == "5.0"


async def test_the_monthly_budget_resets_at_the_start_of_next_month():
    store = _store(_row(budget_monthly_usd=20, key_spent_today_usd=1, key_spent_month_usd=20))

    exc = await _refusal(store)

    assert exc.detail["window"] == "month"
    assert exc.detail["error"] == "API key monthly budget exceeded"
    assert exc.detail["reset_at"] == quota.next_utc_month_start().isoformat()


async def test_a_zero_budget_pauses_the_key():
    exc = await _refusal(_store(_row(budget_daily_usd=0)))

    assert exc.detail["scope"] == "key"


async def test_the_narrowest_spent_limit_is_the_one_reported():
    store = _store(_row(budget_daily_usd=5, key_spent_today_usd=5, cost_usd=100.0))

    exc = await _refusal(store)

    assert exc.detail["scope"] == "key"


async def test_the_account_quota_still_applies_under_a_larger_key_budget():
    store = _store(_row(budget_daily_usd=500, key_spent_today_usd=10, cost_usd=100.0))

    exc = await _refusal(store)

    assert (exc.detail["scope"], exc.detail["error"]) == ("account", "Daily cost quota exceeded")


async def test_a_path_without_the_merged_spend_reads_the_keys_counter():
    """A store without the merged lookup (a test double, older stores) still gates the key."""
    store = MagicMock()
    row = _row(budget_daily_usd=5)
    del row["key_spent_today_usd"], row["key_spent_month_usd"]
    store.get_auth_context_by_key_hash = AsyncMock(return_value=row)
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    store.get_key_spend = AsyncMock(return_value=(5.0, 5.0))
    store.update_key_last_used = AsyncMock()

    exc = await _refusal(store)

    assert exc.detail["scope"] == "key"
    store.get_key_spend.assert_awaited_once_with(21)


async def test_on_behalf_of_ignores_the_service_keys_limits(monkeypatch):
    """The cost is the end user's, so the service key's budget and allowlist do not apply."""
    monkeypatch.setenv("RAG_API_KEY", KEY)
    store = _store(_row(budget_daily_usd=0, allowed_models=["m-a"]))
    store.get_user_by_id = AsyncMock(
        return_value={
            "id": "end-user-1",
            "user_name": "End User",
            "role": "free",
            "status": "active",
            "preferences": None,
            "max_concurrent_requests": None,
        }
    )
    store.get_quota_context_for_user = AsyncMock(
        return_value=[{"quota_daily_cost_usd": 10.0, "quota_monthly_cost_usd": None}]
    )

    ctx = await _verify(store, x_on_behalf_of="end-user-1")

    assert ctx["user_id"] == "end-user-1" and ctx["api_key_id"] is None
    assert KEY_ALLOWED_MODELS_KEY not in ctx


async def test_stored_work_stops_at_its_keys_budget():
    store = _store(_row(budget_monthly_usd=10, key_spent_month_usd=10))

    with pytest.raises(StoredKeyQuotaExceeded) as caught:
        await authenticate_stored_key(hash_api_key(KEY), store)

    assert (caught.value.scope, caught.value.window) == ("key", "month")
    assert caught.value.reset_at == quota.next_utc_month_start()


async def test_stored_work_that_spends_nothing_skips_the_budgets():
    store = _store(_row(budget_daily_usd=0))

    ctx = await authenticate_stored_key(hash_api_key(KEY), store, check_quota=False)

    assert ctx["api_key_id"] == 21


async def test_batch_management_refuses_creation_with_the_keys_refusal():
    store = _store(_row(budget_daily_usd=1, key_spent_today_usd=1))
    request = MagicMock(spec=Request)

    ctx = await verify_batch_management_identity(
        request=request,
        authorization=f"Bearer {KEY}",
        x_api_key=None,
        x_on_behalf_of=None,
        op_store=store,
    )
    assert ctx["quota_exceeded"] is True
    with pytest.raises(HTTPException) as caught:
        enforce_daily_quota(request, ctx)
    await asyncio.sleep(0)

    assert caught.value.detail["scope"] == "key"


async def test_the_listing_context_carries_the_keys_allowlist():
    store = _store(_row(allowed_models=["m-a"]))

    ctx = await optional_verify_api_key(
        request=MagicMock(spec=Request),
        authorization=f"Bearer {KEY}",
        x_api_key=None,
        op_store=store,
    )

    assert ctx is not None and ctx[KEY_ALLOWED_MODELS_KEY] == ["m-a"]
