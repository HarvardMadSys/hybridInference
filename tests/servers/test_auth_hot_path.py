"""The auth hot path answers in one blocking round-trip (issue #185).

``verify_api_key`` used to make three DB round-trips per request: the key
identity lookup, a separate daily-spend read for the quota gate, and an
inline ``last_used_at`` UPDATE. Real stores now fold the spend into the
identity lookup (:meth:`OperationalStore.get_auth_context_with_spend`), and
the ``last_used_at`` write runs as a tracked background task.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from serving.servers.auth import verify_api_key, verify_api_key_for_balance
from serving.storage.base import OperationalStore


@pytest.fixture
def mock_request() -> MagicMock:
    """Return a Request-like mock; verify_api_key does not inspect the object."""
    return MagicMock(spec=Request)


def _key_row(cost_usd: float = 0.0, **overrides) -> dict:
    """An auth-context row as the merged lookup returns it."""
    row = {
        "id": 1,
        "user_id": "user123",
        "user_name": "Test User",
        "quota_daily_cost_usd": 1000.0,
        "role": "free",
        "email": "test@example.com",
        "email_verified": True,
        "cost_usd": cost_usd,
    }
    row.update(overrides)
    return row


def _merged_store(cost_usd: float = 0.0) -> MagicMock:
    """A spec'd store whose merged lookup answers with the given spend.

    ``MagicMock(spec=OperationalStore)`` passes the isinstance gate the auth
    path uses, so these tests exercise the real single-round-trip route.
    """
    store = MagicMock(spec=OperationalStore)
    store.get_auth_context_with_spend = AsyncMock(return_value=_key_row(cost_usd))
    store.get_auth_context_by_key_hash = AsyncMock(return_value=_key_row())
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    store.update_key_last_used = AsyncMock()
    store.get_key_owner_for_audit = AsyncMock(return_value=None)
    return store


async def _settle_background_writes() -> None:
    """Give scheduled tracked tasks their event-loop ticks."""
    for _ in range(3):
        await asyncio.sleep(0)


def _service_target_row() -> dict:
    return {
        "id": "end-user-1",
        "user_name": "End User",
        "role": "free",
        "status": "active",
        "preferences": None,
        "max_concurrent_requests": None,
    }


@pytest.mark.asyncio
async def test_verify_answers_from_one_merged_lookup(monkeypatch, mock_request):
    """The common path reads identity and spend together, never separately."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    store = _merged_store(cost_usd=10.0)

    result = await verify_api_key(
        request=mock_request,
        authorization="Bearer hyi-hot-path",
        x_on_behalf_of=None,
        op_store=store,
        log_store=MagicMock(),
    )
    await _settle_background_writes()

    assert result["user_id"] == "user123"
    assert result["authenticated"] is True
    assert pytest.approx(result["spent_today_usd"], rel=1e-6) == 10.0
    assert pytest.approx(result["quota_remaining_cost_usd"], rel=1e-6) == 990.0
    store.get_auth_context_with_spend.assert_awaited_once()
    store.get_user_cost_today.assert_not_awaited()
    # The last_used write is scheduled, not awaited: it still happens.
    store.update_key_last_used.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_quota_gate_fires_from_the_merged_spend(monkeypatch, mock_request):
    """A merged spend over quota is refused with the same 429 shape."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    store = _merged_store(cost_usd=1000.5)

    with pytest.raises(HTTPException) as exc:
        await verify_api_key(
            request=mock_request,
            authorization="Bearer hyi-hot-path",
            x_on_behalf_of=None,
            op_store=store,
            log_store=MagicMock(),
        )

    assert exc.value.status_code == 429
    assert exc.value.detail["spent_usd"] == pytest.approx(1000.5)
    assert exc.value.headers["Retry-After"]
    store.get_user_cost_today.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_behalf_of_still_counts_the_target_identity(monkeypatch, mock_request):
    """An impersonated identity gets its own counter read, not the caller's."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    service_key = "hyi-rag-service"
    monkeypatch.setenv("RAG_API_KEY", service_key)
    store = _merged_store(cost_usd=1.0)
    store.get_user_by_id = AsyncMock(return_value=_service_target_row())
    store.get_user_cost_today = AsyncMock(return_value=2.5)

    result = await verify_api_key(
        request=mock_request,
        authorization=f"Bearer {service_key}",
        x_on_behalf_of="end-user-1",
        op_store=store,
        log_store=MagicMock(),
    )
    await _settle_background_writes()

    assert result["user_id"] == "end-user-1"
    assert pytest.approx(result["spent_today_usd"], rel=1e-6) == 2.5
    # The caller's merged spend (1.0) must not stand in for the target's.
    store.get_user_cost_today.assert_awaited_once_with("end-user-1")
    store.update_key_last_used.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_unresolvable_key_still_401s_through_the_merged_lookup(monkeypatch, mock_request):
    """A key the merged lookup cannot resolve is the same 401 as before."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    store = _merged_store()
    store.get_auth_context_with_spend = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc:
        await verify_api_key(
            request=mock_request,
            authorization="Bearer hyi-unknown",
            x_on_behalf_of=None,
            op_store=store,
            log_store=MagicMock(),
        )
    await _settle_background_writes()

    assert exc.value.status_code == 401
    store.update_key_last_used.assert_not_awaited()


@pytest.mark.asyncio
async def test_balance_answers_from_the_merged_lookup(monkeypatch, mock_request):
    """The balance check reads identity and spend together, and never bumps last_used."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    store = _merged_store(cost_usd=12.5)

    result = await verify_api_key_for_balance(
        request=mock_request,
        authorization="Bearer hyi-hot-path",
        op_store=store,
    )
    await _settle_background_writes()

    assert result["user_id"] == "user123"
    assert result["authenticated"] is True
    assert pytest.approx(result["spent_today_usd"], rel=1e-6) == 12.5
    store.get_user_cost_today.assert_not_awaited()
    store.update_key_last_used.assert_not_awaited()


@pytest.mark.asyncio
async def test_stores_without_the_merged_lookup_keep_the_two_query_path(monkeypatch, mock_request):
    """A store predating the merged lookup answers identically in two queries."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    store = MagicMock()  # not an OperationalStore: the fallback applies
    store.get_auth_context_by_key_hash = AsyncMock(return_value=_key_row())
    store.get_user_cost_today = AsyncMock(return_value=7.0)
    store.update_key_last_used = AsyncMock()

    result = await verify_api_key(
        request=mock_request,
        authorization="Bearer hyi-legacy-store",
        x_on_behalf_of=None,
        op_store=store,
        log_store=MagicMock(),
    )
    await _settle_background_writes()

    assert result["user_id"] == "user123"
    assert pytest.approx(result["spent_today_usd"], rel=1e-6) == 7.0
    store.get_auth_context_by_key_hash.assert_awaited_once()
    store.get_user_cost_today.assert_awaited_once_with("user123")
    store.update_key_last_used.assert_awaited_once_with(1)
