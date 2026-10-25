"""authenticate_stored_key: a stored key's owner, authenticated again for later work.

The batch worker runs each item long after the request that stored it, and must
stop wherever a live request presenting the same key would be refused. These
tests pin that it answers exactly as ``verify_api_key`` does -- same context,
same refusals -- without any of a request's side effects.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from serving.servers.auth import (
    StoredKeyQuotaExceeded,
    StoredKeyRejected,
    authenticate_stored_key,
    hash_api_key,
    verify_api_key,
)
from serving.storage.base import OperationalStore
from serving.utils import context as req_ctx

KEY = "hyi-batch-owner-key"


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")


def _row(**overrides) -> dict:
    """An auth-context row as the merged lookup returns it."""
    row = {
        "id": 11,
        "user_id": "owner-1",
        "user_name": "Batch Owner",
        "quota_daily_cost_usd": 50.0,
        "role": "internal",
        "email": "owner@example.com",
        "email_verified": True,
        "preferences": {"disabled_models": ["model-x"]},
        "max_concurrent_requests": 7,
        "cost_usd": 12.5,
    }
    row.update(overrides)
    return row


def _store(row: dict | None) -> MagicMock:
    """A spec'd store (so the single-round-trip lookup is the one used)."""
    store = MagicMock(spec=OperationalStore)
    store.get_auth_context_with_spend = AsyncMock(return_value=row)
    store.get_auth_context_by_key_hash = AsyncMock(return_value=row)
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    store.update_key_last_used = AsyncMock()
    store.get_key_owner_for_audit = AsyncMock(return_value=None)
    return store


async def _live(store: MagicMock) -> dict:
    """What verify_api_key answers for KEY against *store*."""
    return await verify_api_key(
        request=MagicMock(spec=Request),
        authorization=f"Bearer {KEY}",
        x_api_key=None,
        x_on_behalf_of=None,
        op_store=store,
        log_store=MagicMock(),
    )


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_context_is_the_one_verify_api_key_returns_for_the_same_row():
    live = await _live(_store(_row()))
    req_ctx.set({})
    store = _store(_row())

    stored = await authenticate_stored_key(hash_api_key(KEY), store)
    await _settle()

    assert stored == live
    assert stored["user_id"] == "owner-1"
    assert stored["role"] == "internal"
    assert stored["disabled_models"] == ["model-x"]
    assert stored["max_concurrent_requests"] == 7
    assert stored["spent_today_usd"] == pytest.approx(12.5)
    assert stored["auth_key_hash"] == hash_api_key(KEY)
    store.get_auth_context_with_spend.assert_awaited_once_with(hash_api_key(KEY))
    # Not a request: nothing written, and no role published on the caller's context.
    store.update_key_last_used.assert_not_awaited()
    assert req_ctx.USER_ROLE not in req_ctx.get()


async def test_stores_without_the_merged_lookup_answer_the_same():
    store = MagicMock()  # not an OperationalStore: the two-query path
    store.get_auth_context_by_key_hash = AsyncMock(return_value=_row())
    store.get_user_cost_today = AsyncMock(return_value=3.0)

    stored = await authenticate_stored_key(hash_api_key(KEY), store)

    assert stored["spent_today_usd"] == pytest.approx(3.0)
    store.get_user_cost_today.assert_awaited_once_with("owner-1")


async def test_revoked_expired_or_deleted_key_is_rejected():
    # The lookup filters key status, expiry and account status: any of them
    # failing reads as no row.
    store = _store(None)

    with pytest.raises(StoredKeyRejected) as exc:
        await authenticate_stored_key(hash_api_key(KEY), store)

    assert "revoked" in exc.value.reason
    with pytest.raises(HTTPException) as live:
        await _live(_store(None))
    assert live.value.status_code == 401


async def test_unverified_email_is_rejected(monkeypatch):
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1")
    row = _row(email_verified=False)

    with pytest.raises(StoredKeyRejected) as exc:
        await authenticate_stored_key(hash_api_key(KEY), _store(row))

    assert "verified" in exc.value.reason
    with pytest.raises(HTTPException) as live:
        await _live(_store(row))
    assert live.value.status_code == 403


async def test_unverified_phone_only_account_is_rejected():
    row = _row(email=None, phone="+15550100", phone_verified=False)

    with pytest.raises(StoredKeyRejected):
        await authenticate_stored_key(hash_api_key(KEY), _store(row))


async def test_spent_quota_reports_when_it_renews():
    row = _row(quota_daily_cost_usd=50.0, cost_usd=49.995)

    with pytest.raises(StoredKeyQuotaExceeded) as exc:
        await authenticate_stored_key(hash_api_key(KEY), _store(row))

    now = datetime.now(UTC)
    reset_at = exc.value.reset_at
    assert reset_at.tzinfo is not None
    assert now < reset_at <= now + timedelta(days=1)
    assert (reset_at.hour, reset_at.minute, reset_at.second) == (0, 0, 0)
    assert exc.value.quota_usd == pytest.approx(50.0)
    assert exc.value.spent_usd == pytest.approx(49.995)
    with pytest.raises(HTTPException) as live:
        await _live(_store(row))
    assert live.value.status_code == 429


async def test_null_quota_takes_the_default():
    stored = await authenticate_stored_key(
        hash_api_key(KEY), _store(_row(quota_daily_cost_usd=None, cost_usd=999.0))
    )

    assert stored["quota_daily_cost_usd"] == pytest.approx(1000.0)


async def test_store_errors_propagate_as_unknown_not_as_refusal():
    store = _store(_row())
    store.get_auth_context_with_spend = AsyncMock(side_effect=ConnectionError("db down"))

    with pytest.raises(ConnectionError):
        await authenticate_stored_key(hash_api_key(KEY), store)


async def test_no_operational_store_is_an_error_not_a_refusal():
    with pytest.raises(RuntimeError):
        await authenticate_stored_key(hash_api_key(KEY), None)


async def test_keyless_work_runs_anonymously_while_auth_is_disabled(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    stored = await authenticate_stored_key(None, None)

    assert stored == {
        "user_id": "anonymous",
        "role": "admin",
        "authenticated": False,
        "is_admin": True,
    }
    assert stored == await verify_api_key(
        request=MagicMock(spec=Request),
        authorization=None,
        x_api_key=None,
        x_on_behalf_of=None,
    )


async def test_keyless_work_is_rejected_once_auth_is_enabled():
    with pytest.raises(StoredKeyRejected) as exc:
        await authenticate_stored_key(None, _store(_row()))

    assert "without an API key" in exc.value.reason


async def test_keyed_work_is_anonymous_while_auth_is_disabled(monkeypatch):
    """As for a live request: with auth off, no key is looked up at all."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    store = _store(_row())

    stored = await authenticate_stored_key(hash_api_key(KEY), store)

    assert stored["user_id"] == "anonymous"
    store.get_auth_context_with_spend.assert_not_awaited()
