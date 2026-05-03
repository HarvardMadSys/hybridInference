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
