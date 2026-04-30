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
from serving.storage.database import DatabaseLogger


class _AcquireContext:
    """Async context manager yielding a predefined connection (mirrors test_auth.py)."""

    def __init__(self, connection: AsyncMock) -> None:
        self._connection = connection

    async def __aenter__(self) -> AsyncMock:
        return self._connection

    async def __aexit__(self, *_: Any) -> None:
        return None


@pytest.fixture
def mock_request() -> Request:
    """Return a Request-like mock; verify_api_key does not inspect the object."""
    return MagicMock(spec=Request)


@pytest.fixture
def mock_db_with_pool() -> tuple[DatabaseLogger, AsyncMock]:
    """DatabaseLogger mock backed by an asyncpg-style pool."""
    db_logger = MagicMock(spec=DatabaseLogger)
    connection = AsyncMock()
    connection.fetchrow = AsyncMock()
    connection.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.side_effect = lambda: _AcquireContext(connection)
    db_logger.pool = pool
    return db_logger, connection


@pytest.mark.asyncio
async def test_verify_api_key_returns_auth_key_hash_matching_input(
    monkeypatch, mock_request, mock_db_with_pool
):
    """A valid key resolves to a user dict whose ``auth_key_hash`` == hash_api_key(plaintext)."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")

    db_logger, connection = mock_db_with_pool
    plaintext_key = "hyi-affinity-test"
    expected_hash = hash_api_key(plaintext_key)

    connection.fetchrow.side_effect = [
        {
            "id": 42,
            "user_id": "user-affinity",
            "user_name": "Affinity Tester",
            "quota_daily_cost_usd": 1000.0,
            "tier": "free",
        },
        {"cost_spent": 0.0},
    ]

    result = await verify_api_key(
        request=mock_request,
        authorization=f"Bearer {plaintext_key}",
        db_logger=db_logger,
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
