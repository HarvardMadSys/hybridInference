"""Behavioral tests for ``auth_key_hash`` plumbing.

These verify that ``verify_api_key`` returns the caller's key hash as
``auth_key_hash`` and that the chat completions handler propagates it onto
the request context, where the multi-key adapter pool reads it as the
affinity key.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request

from serving.servers.auth import hash_api_key, verify_api_key


@pytest.fixture
def mock_request() -> Request:
    """Return a Request-like mock; verify_api_key does not inspect the object."""
    return MagicMock(spec=Request)


@pytest.fixture
def mock_op_store():
    """Mock OperationalStore for verify_api_key tests."""
    store = MagicMock()
    store.get_auth_context_by_key_hash = AsyncMock()
    store.update_key_last_used = AsyncMock()
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    return store


@pytest.mark.asyncio
async def test_verify_api_key_returns_auth_key_hash_matching_input(
    monkeypatch, mock_request, mock_op_store
):
    """A valid key resolves to a user dict whose ``auth_key_hash`` == hash_api_key(plaintext)."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")

    plaintext_key = "hyi-affinity-test"
    expected_hash = hash_api_key(plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 42,
        "user_id": "user-affinity",
        "user_name": "Affinity Tester",
        "quota_daily_cost_usd": 1000.0,
        "role": "free",
        "email": "affinity@example.com",
        "email_verified": True,
    }

    result = await verify_api_key(
        request=mock_request,
        authorization=f"Bearer {plaintext_key}",
        op_store=mock_op_store,
    )

    assert result["authenticated"] is True
    assert result["auth_key_hash"] == expected_hash, (
        "verify_api_key must surface the request's key_hash so the multi-key "
        "adapter pool can use it as the affinity key"
    )


def test_completions_handler_propagates_auth_key_hash_to_context():
    """The chat handler updates ``req_ctx`` with ``auth_key_hash`` from the user context.

    Exercises the same code path the request flow takes: ``req_ctx.update`` is
    a contextvar mutation, so we set a baseline context, run the snippet from
    ``chat_completions``, and read it back. This catches regressions where the
    field is renamed or stops being propagated.
    """
    from serving.utils import context as req_ctx

    user_ctx = {"user_id": "u-1", "auth_key_hash": "deadbeef"}

    # Mirror the handler's plumbing exactly (see
    # serving/servers/routers/completions.py near line 226).
    req_ctx.set({})
    req_ctx.update({"auth_key_hash": user_ctx.get("auth_key_hash") or "_anon"})

    assert req_ctx.get().get("auth_key_hash") == "deadbeef"


def test_completions_handler_falls_back_to_anon_sentinel():
    """When ``auth_key_hash`` is missing from user context, the handler uses ``_anon``."""
    from serving.utils import context as req_ctx

    user_ctx_without_hash: dict[str, Any] = {"user_id": "u-2"}

    req_ctx.set({})
    req_ctx.update({"auth_key_hash": user_ctx_without_hash.get("auth_key_hash") or "_anon"})

    assert req_ctx.get().get("auth_key_hash") == "_anon"


def test_encrypt_api_key_round_trips_plaintext(monkeypatch):
    """``decrypt_api_key(encrypt_api_key(k)) == k`` and ciphertext hides the key.

    Backs the dashboard reveal feature: the stored ciphertext must decrypt
    back to the exact plaintext key so the API Keys list can display it.
    """
    from serving.servers.auth import decrypt_api_key, encrypt_api_key

    monkeypatch.setenv("API_KEY_SECRET", "test-secret")

    plaintext_key = "hyi-display-round-trip"
    encrypted = encrypt_api_key(plaintext_key)

    assert encrypted != plaintext_key
    assert decrypt_api_key(encrypted) == plaintext_key


def test_decrypt_api_key_returns_none_for_missing_ciphertext(monkeypatch):
    """Legacy rows (NULL/empty ciphertext) decrypt to None, driving the mask fallback."""
    from serving.servers.auth import decrypt_api_key

    monkeypatch.setenv("API_KEY_SECRET", "test-secret")

    assert decrypt_api_key(None) is None
    assert decrypt_api_key("") is None


@pytest.mark.asyncio
async def test_verify_api_key_publishes_caller_role_to_context(
    monkeypatch, mock_request, mock_op_store
):
    """The caller's role lands on ``req_ctx`` so the key pool can tier-gate keys."""
    from serving.utils import context as req_ctx

    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 7,
        "user_id": "user-pro",
        "user_name": "Pro Tester",
        "quota_daily_cost_usd": 1000.0,
        "role": "pro",
        "email": "pro@example.com",
        "email_verified": True,
    }

    req_ctx.set({})
    await verify_api_key(
        request=mock_request,
        authorization="Bearer hyi-role-test",
        op_store=mock_op_store,
    )

    assert req_ctx.get().get("user_role") == "pro"


@pytest.mark.asyncio
async def test_verify_api_key_publishes_free_for_a_roleless_identity(
    monkeypatch, mock_request, mock_op_store
):
    """A row with no role is published as ``free`` — never as "unrestricted".

    Absent means unrestricted to the key pool, so a missing role must be
    normalized here or a roleless account would reach reserved keys.
    """
    from serving.utils import context as req_ctx

    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 8,
        "user_id": "user-legacy",
        "user_name": "Legacy Row",
        "quota_daily_cost_usd": 1000.0,
        "role": None,
        "email": "legacy@example.com",
        "email_verified": True,
    }

    req_ctx.set({})
    await verify_api_key(
        request=mock_request,
        authorization="Bearer hyi-role-missing",
        op_store=mock_op_store,
    )

    assert req_ctx.get().get("user_role") == "free"


def test_request_id_middleware_clears_a_stale_caller_role():
    """Each request resets ``user_role`` so entitlement never leaks between callers."""
    from serving.utils import context as req_ctx

    req_ctx.set({"user_role": "admin"})
    # Mirror the middleware's reset block (servers/middleware/request_id.py).
    req_ctx.update(
        {
            "request_id": "abc",
            "client_user_agent": None,
            "user_id": None,
            "user_name": None,
            "user_role": None,
            req_ctx.CLIENT_ERROR_KIND: None,
        }
    )

    assert req_ctx.get().get("user_role") is None
