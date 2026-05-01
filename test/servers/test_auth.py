"""Unit tests for verify_api_key authentication and quota logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from serving.servers.auth import decrypt_api_key, encrypt_api_key, hash_api_key, verify_api_key


@pytest.fixture
def mock_request() -> Request:
    """Return a Request-like mock; verify_api_key does not inspect the object."""
    return MagicMock(spec=Request)


@pytest.fixture
def mock_op_store():
    """Create a mock OperationalStore for verify_api_key tests."""
    store = MagicMock()
    store.get_auth_context_by_key_hash = AsyncMock()
    store.update_key_last_used = AsyncMock()
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    return store


@pytest.fixture
def mock_ls():
    """Create a mock LogStore for verify_api_key tests."""
    store = MagicMock()
    store.get_user_cost_today = AsyncMock(return_value=0.0)
    return store


def _hashed_key(monkeypatch, plaintext: str) -> str:
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    return hash_api_key(plaintext)


@pytest.mark.asyncio
async def test_auth_disabled_returns_anonymous(monkeypatch, mock_request):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    result = await verify_api_key(request=mock_request)
    assert result == {
        "user_id": "anonymous",
        "tier": "free",
        "role": "admin",
        "authenticated": False,
        "is_admin": True,
    }


@pytest.mark.asyncio
async def test_auth_defaults_enabled(monkeypatch, mock_request, mock_op_store, mock_ls):
    """Missing USER_AUTH_ENABLED fails closed instead of granting anonymous admin."""
    monkeypatch.delenv("USER_AUTH_ENABLED", raising=False)
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")

    with pytest.raises(HTTPException) as exc:
        await verify_api_key(
            request=mock_request,
            authorization=None,
            x_api_key=None,
            op_store=mock_op_store,
            log_store=mock_ls,
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_auth_missing_headers_returns_401(monkeypatch, mock_request, mock_op_store, mock_ls):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")

    with pytest.raises(HTTPException) as exc:
        await verify_api_key(
            request=mock_request,
            authorization=None,
            x_api_key=None,
            op_store=mock_op_store,
            log_store=mock_ls,
        )

    assert exc.value.status_code == 401
    assert "Missing API key" in str(exc.value.detail)



def test_api_key_encryption_round_trip(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    plaintext_key = "hyi-valid-key"

    encrypted_key = encrypt_api_key(plaintext_key)

    assert encrypted_key != plaintext_key
    assert decrypt_api_key(encrypted_key) == plaintext_key



@pytest.mark.asyncio
async def test_auth_authorization_bearer_valid(monkeypatch, mock_request, mock_op_store, mock_ls):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    plaintext_key = "hyi-valid-key"
    hashed_key = _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 1,
        "user_id": "user123",
        "user_name": "Test User",
        "quota_daily_cost_usd": 1000.0,
        "tier": "free",
        "role": "free",
        "email": "test@example.com",
    }
    mock_op_store.get_user_cost_today.return_value = 10.0

    result = await verify_api_key(
        request=mock_request,
        authorization=f"Bearer {plaintext_key}",
        op_store=mock_op_store,
        log_store=mock_ls,
    )

    assert result["user_id"] == "user123"
    assert result["authenticated"] is True
    assert pytest.approx(result["quota_remaining_cost_usd"], rel=1e-6) == 990.0
    mock_op_store.get_auth_context_by_key_hash.assert_awaited_once_with(hashed_key)
    mock_op_store.get_user_cost_today.assert_awaited_once_with("user123")
    mock_op_store.update_key_last_used.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_auth_x_api_key_header_valid(monkeypatch, mock_request, mock_op_store, mock_ls):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    plaintext_key = "hyi-x-header"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 2,
        "user_id": "user456",
        "user_name": "X Header",
        "quota_daily_cost_usd": 500.0,
        "tier": "pro",
        "role": "free",
        "email": "x@example.com",
    }
    mock_op_store.get_user_cost_today.return_value = 100.0

    result = await verify_api_key(
        request=mock_request,
        authorization=None,
        x_api_key=plaintext_key,
        op_store=mock_op_store,
        log_store=mock_ls,
    )

    assert result["user_id"] == "user456"
    assert result["authenticated"] is True
    assert pytest.approx(result["quota_remaining_cost_usd"], rel=1e-6) == 400.0


@pytest.mark.asyncio
async def test_auth_unverified_user_key_returns_403(monkeypatch, mock_request, mock_op_store, mock_ls):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1")
    plaintext_key = "hyi-unverified"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 7,
        "user_id": "unverified-user",
        "user_name": "Unverified",
        "quota_daily_cost_usd": 1000.0,
        "tier": "free",
        "email": "unverified@test.example.com",
        "email_verified": False,
    }

    with pytest.raises(HTTPException) as exc:
        await verify_api_key(
            request=mock_request,
            authorization=f"Bearer {plaintext_key}",
            op_store=mock_op_store,
            log_store=mock_ls,
        )

    assert exc.value.status_code == 403
    mock_op_store.get_auth_context_by_key_hash.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_invalid_key_hash_returns_401(monkeypatch, mock_request, mock_op_store, mock_ls):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    plaintext_key = "hyi-invalid"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = None

    with pytest.raises(HTTPException) as exc:
        await verify_api_key(
            request=mock_request,
            authorization=f"Bearer {plaintext_key}",
            op_store=mock_op_store,
            log_store=mock_ls,
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_auth_quota_exceeded_returns_429(monkeypatch, mock_request, mock_op_store, mock_ls):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    plaintext_key = "hyi-over-quota"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 3,
        "user_id": "heavy-user",
        "user_name": "Over Quota",
        "quota_daily_cost_usd": 1000.0,
        "tier": "free",
        "role": "free",
        "email": "heavy@example.com",
    }
    mock_op_store.get_user_cost_today.return_value = 1000.0

    with pytest.raises(HTTPException) as exc:
        await verify_api_key(
            request=mock_request,
            authorization=f"Bearer {plaintext_key}",
            op_store=mock_op_store,
            log_store=mock_ls,
        )

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"]
    assert exc.value.headers["X-RateLimit-Limit-Cost"] == "1000.0"
    assert exc.value.headers["X-RateLimit-Reset"]
    assert exc.value.detail["remaining_usd"] == 0
    assert exc.value.detail["reset_at"]
    assert exc.value.detail["contact_email"] == "admin@freeinference.org"


@pytest.mark.asyncio
async def test_auth_quota_null_uses_default_1000(monkeypatch, mock_request, mock_op_store, mock_ls):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    plaintext_key = "hyi-null-quota"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 4,
        "user_id": "user-null-quota",
        "user_name": "Null Quota",
        "quota_daily_cost_usd": None,
        "tier": "free",
        "role": "free",
        "email": "null@example.com",
    }
    mock_op_store.get_user_cost_today.return_value = 0.5

    result = await verify_api_key(
        request=mock_request,
        authorization=f"Bearer {plaintext_key}",
        op_store=mock_op_store,
        log_store=mock_ls,
    )

    assert result["user_id"] == "user-null-quota"
    assert pytest.approx(result["quota_remaining_cost_usd"], rel=1e-6) == 999.5


@pytest.mark.asyncio
async def test_auth_missing_secret_raises_error(monkeypatch, mock_request, mock_op_store, mock_ls):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.delenv("API_KEY_SECRET", raising=False)
    plaintext_key = "hyi-no-secret"

    with pytest.raises(ValueError):
        await verify_api_key(
            request=mock_request,
            authorization=f"Bearer {plaintext_key}",
            op_store=mock_op_store,
            log_store=mock_ls,
        )


@pytest.mark.asyncio
async def test_auth_updates_last_used_at(monkeypatch, mock_request, mock_op_store, mock_ls):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    plaintext_key = "hyi-update-last-used"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 6,
        "user_id": "user-updated",
        "user_name": "Updated",
        "quota_daily_cost_usd": 200.0,
        "tier": "free",
        "role": "free",
        "email": "updated@example.com",
    }
    mock_op_store.get_user_cost_today.return_value = 50.0

    await verify_api_key(
        request=mock_request,
        authorization=f"Bearer {plaintext_key}",
        op_store=mock_op_store,
        log_store=mock_ls,
    )

    mock_op_store.update_key_last_used.assert_awaited_once_with(6)
