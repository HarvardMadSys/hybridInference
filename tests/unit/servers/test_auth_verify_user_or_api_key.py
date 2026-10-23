"""Tests for the JWT-or-API-key dependency used by /user/usage."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from serving.servers import auth


@pytest.mark.asyncio
async def test_api_key_takes_read_only_lookup_path(monkeypatch):
    expected = {"user_id": "u1", "role": "free", "max_concurrent_requests": 7}
    resolver = AsyncMock(return_value=expected)
    jwt_resolver = AsyncMock()
    monkeypatch.setattr(auth, "optional_verify_api_key", resolver)
    monkeypatch.setattr(auth, "get_current_user", jwt_resolver)

    user = await auth.verify_user_or_api_key(
        MagicMock(),
        authorization="Bearer hyi-abc",
        x_api_key=None,
        op_store=MagicMock(),
    )

    assert user == expected
    resolver.assert_awaited_once()
    jwt_resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_x_api_key_header_is_accepted(monkeypatch):
    expected = {"user_id": "u1", "role": "free"}
    resolver = AsyncMock(return_value=expected)
    monkeypatch.setattr(auth, "optional_verify_api_key", resolver)

    user = await auth.verify_user_or_api_key(
        MagicMock(),
        authorization=None,
        x_api_key="hyi-abc",
        op_store=MagicMock(),
    )

    assert user == expected


@pytest.mark.asyncio
async def test_invalid_api_key_raises_401(monkeypatch):
    monkeypatch.setattr(auth, "optional_verify_api_key", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc:
        await auth.verify_user_or_api_key(
            MagicMock(),
            authorization="Bearer hyi-revoked",
            x_api_key=None,
            op_store=MagicMock(),
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_non_api_key_bearer_falls_through_to_jwt(monkeypatch):
    expected = {"user_id": "u1", "role": "pro", "is_admin": False}
    jwt_resolver = AsyncMock(return_value=expected)
    monkeypatch.setattr(auth, "get_current_user", jwt_resolver)
    monkeypatch.setattr(auth, "optional_verify_api_key", AsyncMock())
    op_store = MagicMock()

    user = await auth.verify_user_or_api_key(
        MagicMock(),
        authorization="Bearer eyJhbGciOi",
        x_api_key=None,
        op_store=op_store,
    )

    assert user == expected
    jwt_resolver.assert_awaited_once_with(authorization="Bearer eyJhbGciOi", op_store=op_store)
