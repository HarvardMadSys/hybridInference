"""Tests confirming new API keys are seeded with the role-specific daily quota.

Both call sites in user_routes.py are exercised:
- create_api_key  (POST /user/api-keys)
- regenerate_api_key (POST /user/api-keys/regenerate)
"""

from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import serving.servers.routers.user_routes as user_routes
from serving.servers.routers.user_routes import create_api_key, regenerate_api_key

_FAKE_API_KEY = "hyi-" + "a" * 44
_FAKE_KEY_HASH = "fakehash123"


@contextmanager
def _patch_auth():
    """Patch crypto helpers that require env secrets."""
    with (
        patch.object(user_routes, "generate_api_key", return_value=_FAKE_API_KEY),
        patch.object(user_routes, "hash_api_key", return_value=_FAKE_KEY_HASH),
        patch.object(user_routes, "log_admin_action", new_callable=AsyncMock),
    ):
        yield


def _make_rt(quota_value: float = 250.0) -> AsyncMock:
    """Return a mock RuntimeSettings whose get_float returns *quota_value*."""
    rt = AsyncMock()
    rt.get_float.return_value = quota_value
    return rt


def _make_op_store(*, has_active_key: bool = False) -> AsyncMock:
    """Return a minimal mock OperationalStore."""
    store = AsyncMock()
    store.get_active_key_by_account.return_value = (
        {"key_prefix": "abc123456789"} if has_active_key else None
    )
    store.create_key = AsyncMock()
    store.revoke_key = AsyncMock()
    return store


def _make_current_user(role: str = "pro") -> dict:
    return {
        "user_id": "user-123",
        "role": role,
        "email_verified": True,
        "is_admin": False,
    }


def _make_request() -> MagicMock:
    req = MagicMock()
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _make_db_logger() -> AsyncMock:
    db_logger = AsyncMock()
    return db_logger


# ---------------------------------------------------------------------------
# create_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_api_key_uses_role_specific_quota():
    """create_api_key must seed the key with the role-specific quota."""
    rt = _make_rt(250.0)
    op_store = _make_op_store(has_active_key=False)
    current_user = _make_current_user(role="pro")
    request = _make_request()
    db_logger = _make_db_logger()

    with _patch_auth(), patch.object(user_routes, "get_runtime_settings_instance", return_value=rt):
        await create_api_key(
            request=request,
            current_user=current_user,
            op_store=op_store,
            db_logger=db_logger,
        )

    rt.get_float.assert_awaited_with("user_daily_quota_pro")
    op_store.create_key.assert_awaited_once()
    call_kwargs = op_store.create_key.call_args.kwargs
    assert call_kwargs["quota_daily_cost_usd"] == Decimal("250.0")


@pytest.mark.asyncio
async def test_create_api_key_free_role_uses_role_specific_quota():
    """create_api_key respects the 'free' role key."""
    rt = _make_rt(75.0)
    op_store = _make_op_store(has_active_key=False)
    current_user = _make_current_user(role="free")
    request = _make_request()
    db_logger = _make_db_logger()

    with _patch_auth(), patch.object(user_routes, "get_runtime_settings_instance", return_value=rt):
        await create_api_key(
            request=request,
            current_user=current_user,
            op_store=op_store,
            db_logger=db_logger,
        )

    rt.get_float.assert_awaited_with("user_daily_quota_free")
    call_kwargs = op_store.create_key.call_args.kwargs
    assert call_kwargs["quota_daily_cost_usd"] == Decimal("75.0")


# ---------------------------------------------------------------------------
# regenerate_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_api_key_uses_role_specific_quota():
    """regenerate_api_key must seed the new key with the role-specific quota."""
    rt = _make_rt(250.0)
    op_store = _make_op_store(has_active_key=True)
    current_user = _make_current_user(role="pro")
    request = _make_request()
    db_logger = _make_db_logger()

    with _patch_auth(), patch.object(user_routes, "get_runtime_settings_instance", return_value=rt):
        await regenerate_api_key(
            request=request,
            current_user=current_user,
            op_store=op_store,
            db_logger=db_logger,
        )

    rt.get_float.assert_awaited_with("user_daily_quota_pro")
    op_store.create_key.assert_awaited_once()
    call_kwargs = op_store.create_key.call_args.kwargs
    assert call_kwargs["quota_daily_cost_usd"] == Decimal("250.0")


@pytest.mark.asyncio
async def test_regenerate_api_key_internal_role_uses_role_specific_quota():
    """regenerate_api_key respects the 'internal' role key."""
    rt = _make_rt(1000.0)
    op_store = _make_op_store(has_active_key=True)
    current_user = _make_current_user(role="internal")
    request = _make_request()
    db_logger = _make_db_logger()

    with _patch_auth(), patch.object(user_routes, "get_runtime_settings_instance", return_value=rt):
        await regenerate_api_key(
            request=request,
            current_user=current_user,
            op_store=op_store,
            db_logger=db_logger,
        )

    rt.get_float.assert_awaited_with("user_daily_quota_internal")
    call_kwargs = op_store.create_key.call_args.kwargs
    assert call_kwargs["quota_daily_cost_usd"] == Decimal("1000.0")
