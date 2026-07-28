"""Unit tests for verify_api_key authentication and quota logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from serving.servers.auth import (
    decrypt_api_key,
    encrypt_api_key,
    hash_api_key,
    optional_verify_api_key,
    verify_api_key,
    verify_api_key_for_balance,
)


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
    result = await verify_api_key(request=mock_request, authorization=None, x_api_key=None)
    assert result == {
        "user_id": "anonymous",
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
async def test_auth_unverified_user_key_returns_403(
    monkeypatch, mock_request, mock_op_store, mock_ls
):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1")
    plaintext_key = "hyi-unverified"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 7,
        "user_id": "unverified-user",
        "user_name": "Unverified",
        "quota_daily_cost_usd": 1000.0,
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
    # The support address follows the site identity; unconfigured means none,
    # and the message says who to ask instead of naming a stranger's inbox.
    assert exc.value.detail["contact_email"] == ""
    assert "operator of this deployment" in exc.value.detail["message"]


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
    monkeypatch.setenv("API_KEY_SECRET", "")
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


# ---------------------------------------------------------------------------
# RuntimeSettings wiring: signup_require_email_verification
# ---------------------------------------------------------------------------


@pytest.fixture
def _rt_store():
    store = MagicMock()
    store.get_setting = AsyncMock(return_value=None)
    return store


async def test_verify_api_key_respects_runtime_flag_disabled(
    monkeypatch, mock_request, mock_op_store, mock_ls, _rt_store
):
    """verify_api_key passes an unverified user when the flag is off via RuntimeSettings."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1")
    plaintext_key = "hyi-rt-flag-off"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 10,
        "user_id": "unverified-rt",
        "user_name": "RT Test",
        "quota_daily_cost_usd": 1000.0,
        "role": "free",
        "email": "rt@test.example.com",
        "email_verified": False,
    }
    mock_op_store.get_user_cost_today.return_value = 0.0

    import serving.config.runtime_settings as _mod
    from serving.config.runtime_settings import RuntimeSettings

    rt = RuntimeSettings(_rt_store, ttl=30.0)
    rt._cache["signup_require_email_verification"] = (
        __import__("time").monotonic(),
        False,
    )
    old = _mod._runtime_settings
    try:
        _mod._runtime_settings = rt
        result = await verify_api_key(
            request=mock_request,
            authorization=f"Bearer {plaintext_key}",
            op_store=mock_op_store,
            log_store=mock_ls,
        )
    finally:
        _mod._runtime_settings = old

    assert result["user_id"] == "unverified-rt"
    assert result["authenticated"] is True


async def test_verify_api_key_respects_runtime_flag_enabled(
    monkeypatch, mock_request, mock_op_store, mock_ls, _rt_store
):
    """verify_api_key blocks an unverified user when the flag is on via RuntimeSettings."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.delenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", raising=False)
    plaintext_key = "hyi-rt-flag-on"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 11,
        "user_id": "unverified-rt2",
        "user_name": "RT Test2",
        "quota_daily_cost_usd": 1000.0,
        "role": "free",
        "email": "rt2@test.example.com",
        "email_verified": False,
    }
    mock_op_store.get_user_cost_today.return_value = 0.0

    import serving.config.runtime_settings as _mod
    from serving.config.runtime_settings import RuntimeSettings

    rt = RuntimeSettings(_rt_store, ttl=30.0)
    rt._cache["signup_require_email_verification"] = (
        __import__("time").monotonic(),
        True,
    )
    old = _mod._runtime_settings
    try:
        _mod._runtime_settings = rt
        with pytest.raises(HTTPException) as exc:
            await verify_api_key(
                request=mock_request,
                authorization=f"Bearer {plaintext_key}",
                op_store=mock_op_store,
                log_store=mock_ls,
            )
    finally:
        _mod._runtime_settings = old

    assert exc.value.status_code == 403


async def test_optional_verify_api_key_respects_runtime_flag_disabled(
    monkeypatch, mock_request, mock_op_store, _rt_store
):
    """optional_verify_api_key passes an unverified user when the flag is off via RuntimeSettings."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", "1")
    plaintext_key = "hyi-opt-rt-off"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_lightweight = AsyncMock(
        return_value={
            "user_id": "opt-unverified",
            "role": "free",
            "email": "opt@test.example.com",
            "email_verified": False,
        }
    )

    import serving.config.runtime_settings as _mod
    from serving.config.runtime_settings import RuntimeSettings

    rt = RuntimeSettings(_rt_store, ttl=30.0)
    rt._cache["signup_require_email_verification"] = (
        __import__("time").monotonic(),
        False,
    )
    old = _mod._runtime_settings
    try:
        _mod._runtime_settings = rt
        result = await optional_verify_api_key(
            request=mock_request,
            authorization=f"Bearer {plaintext_key}",
            op_store=mock_op_store,
        )
    finally:
        _mod._runtime_settings = old

    assert result is not None
    assert result["user_id"] == "opt-unverified"


async def test_optional_verify_api_key_respects_runtime_flag_enabled(
    monkeypatch, mock_request, mock_op_store, _rt_store
):
    """optional_verify_api_key returns None for unverified user when flag is on via RuntimeSettings."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.delenv("SIGNUP_REQUIRE_EMAIL_VERIFICATION", raising=False)
    plaintext_key = "hyi-opt-rt-on"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_lightweight = AsyncMock(
        return_value={
            "user_id": "opt-unverified2",
            "role": "free",
            "email": "opt2@test.example.com",
            "email_verified": False,
        }
    )

    import serving.config.runtime_settings as _mod
    from serving.config.runtime_settings import RuntimeSettings

    rt = RuntimeSettings(_rt_store, ttl=30.0)
    rt._cache["signup_require_email_verification"] = (
        __import__("time").monotonic(),
        True,
    )
    old = _mod._runtime_settings
    try:
        _mod._runtime_settings = rt
        result = await optional_verify_api_key(
            request=mock_request,
            authorization=f"Bearer {plaintext_key}",
            op_store=mock_op_store,
        )
    finally:
        _mod._runtime_settings = old

    assert result is None


@pytest.mark.asyncio
async def test_balance_auth_disabled_returns_anonymous(monkeypatch, mock_request):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    result = await verify_api_key_for_balance(request=mock_request)
    assert result == {"user_id": "anonymous", "authenticated": False}


@pytest.mark.asyncio
async def test_balance_missing_headers_returns_401(monkeypatch, mock_request, mock_op_store):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")

    with pytest.raises(HTTPException) as exc:
        await verify_api_key_for_balance(
            request=mock_request, authorization=None, x_api_key=None, op_store=mock_op_store
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_balance_invalid_key_returns_401(monkeypatch, mock_request, mock_op_store):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    monkeypatch.setenv("API_KEY_SECRET", "test-secret")
    mock_op_store.get_auth_context_by_key_hash.return_value = None

    with pytest.raises(HTTPException) as exc:
        await verify_api_key_for_balance(
            request=mock_request,
            authorization="Bearer hyi-does-not-exist",
            op_store=mock_op_store,
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_balance_returns_quota_and_spend_without_gating(
    monkeypatch, mock_request, mock_op_store
):
    """Unlike verify_api_key, a balance check must succeed even over quota."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    plaintext_key = "hyi-over-quota-balance"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 9,
        "user_id": "heavy-user",
        "user_name": "Over Quota",
        "quota_daily_cost_usd": 10.0,
        "role": "free",
        "email": "heavy@example.com",
    }
    mock_op_store.get_user_cost_today.return_value = 12.5

    result = await verify_api_key_for_balance(
        request=mock_request,
        authorization=f"Bearer {plaintext_key}",
        op_store=mock_op_store,
    )

    assert result["user_id"] == "heavy-user"
    assert result["authenticated"] is True
    assert result["quota_daily_cost_usd"] == 10.0
    assert result["spent_today_usd"] == 12.5
    mock_op_store.update_key_last_used.assert_not_awaited()


@pytest.mark.asyncio
async def test_balance_quota_null_uses_default_1000(monkeypatch, mock_request, mock_op_store):
    monkeypatch.setenv("USER_AUTH_ENABLED", "1")
    plaintext_key = "hyi-null-quota-balance"
    _hashed_key(monkeypatch, plaintext_key)

    mock_op_store.get_auth_context_by_key_hash.return_value = {
        "id": 10,
        "user_id": "user-null-quota",
        "user_name": "Null Quota",
        "quota_daily_cost_usd": None,
        "role": "free",
        "email": "null@example.com",
    }
    mock_op_store.get_user_cost_today.return_value = 0.5

    result = await verify_api_key_for_balance(
        request=mock_request,
        authorization=f"Bearer {plaintext_key}",
        op_store=mock_op_store,
    )

    assert result["quota_daily_cost_usd"] == 1000.0
    assert result["spent_today_usd"] == 0.5
