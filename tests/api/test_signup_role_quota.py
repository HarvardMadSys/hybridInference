"""Tests confirming an account's first API key is seeded with the role-specific quota.

The quota is the account's: ``create_api_key`` (POST /user/api-keys) offers the
role default, which the store applies only to a first key, while a further key
inherits the account's. ``regenerate_api_key`` rotates a key in place and keeps
the account's quota rather than re-seeding the role default.
"""

from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import serving.servers.routers.user_routes as user_routes
from serving.exceptions import APIKeyLimitReachedError, DuplicateAPIKeyError
from serving.servers.routers.user_routes import create_api_key, regenerate_api_key

_FAKE_API_KEY = "hyi-" + "a" * 44
_FAKE_KEY_HASH = "fakehash123"


@pytest.fixture(autouse=True)
def _api_key_secret(monkeypatch):
    """Make these tests self-contained w.r.t. ``API_KEY_SECRET``.

    ``create_api_key`` / ``regenerate_api_key`` reach the real
    ``_api_key_cipher`` (only ``generate_api_key`` and ``hash_api_key`` are
    patched), which reads ``API_KEY_SECRET`` through the ``lru_cache``-d
    ``get_settings``. Set the secret and clear the settings cache so it is
    picked up regardless of test order; otherwise these tests pass only when
    another test happens to leave the secret in the environment, which flakes
    under xdist sharding.
    """
    from serving.config.settings import get_settings

    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    get_settings.cache_clear()


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
    store.get_key_by_account_or_user.return_value = (
        {"key_prefix": "abc123456789"} if has_active_key else None
    )
    store.get_default_project.return_value = {"id": "proj_default", "name": "Default"}
    store.create_key = AsyncMock()
    store.revoke_key = AsyncMock()
    return store


def _make_current_user(role: str = "pro") -> dict:
    return {
        "user_id": "user-123",
        "role": role,
        "email": "quota-user@example.test",
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


@pytest.mark.asyncio
async def test_create_api_key_inherits_the_account_quota_under_the_cap():
    """An account with a key gets another: the store inherits its quota and enforces the cap."""
    rt = _make_rt(250.0)
    op_store = _make_op_store(has_active_key=True)
    current_user = _make_current_user(role="pro")

    with _patch_auth(), patch.object(user_routes, "get_runtime_settings_instance", return_value=rt):
        await create_api_key(
            request=_make_request(),
            current_user=current_user,
            op_store=op_store,
            db_logger=_make_db_logger(),
        )

    call_kwargs = op_store.create_key.call_args.kwargs
    assert call_kwargs["inherit_account_quota"] is True
    assert call_kwargs["max_active_keys"] == user_routes.MAX_ACTIVE_API_KEYS_PER_ACCOUNT


@pytest.mark.asyncio
async def test_create_api_key_at_the_cap_returns_409():
    rt = _make_rt(250.0)
    op_store = _make_op_store(has_active_key=True)
    op_store.create_key = AsyncMock(side_effect=APIKeyLimitReachedError(3))

    with (
        _patch_auth(),
        patch.object(user_routes, "get_runtime_settings_instance", return_value=rt),
        pytest.raises(HTTPException) as exc_info,
    ):
        await create_api_key(
            request=_make_request(),
            current_user=_make_current_user(role="pro"),
            op_store=op_store,
            db_logger=_make_db_logger(),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_create_api_key_propagates_duplicate_on_insert_race():
    """A unique-violation surfaced by create_key must bubble as
    DuplicateAPIKeyError (→ global 409 handler), not be swallowed or become 500.

    Covers the concurrent-create race and any pre-check miss.
    """
    rt = _make_rt(250.0)
    op_store = _make_op_store(has_active_key=False)
    op_store.create_key = AsyncMock(side_effect=DuplicateAPIKeyError("dup"))
    current_user = _make_current_user(role="pro")
    request = _make_request()
    db_logger = _make_db_logger()

    with (
        _patch_auth(),
        patch.object(user_routes, "get_runtime_settings_instance", return_value=rt),
        pytest.raises(DuplicateAPIKeyError),
    ):
        await create_api_key(
            request=request,
            current_user=current_user,
            op_store=op_store,
            db_logger=db_logger,
        )


# ---------------------------------------------------------------------------
# regenerate_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_api_key_rotates_without_reseeding_the_role_quota():
    """A rotation keeps the account's quota: no role default is read or written."""
    rt = _make_rt(250.0)
    op_store = _make_op_store(has_active_key=True)
    op_store.rotate_key.return_value = {
        "id": 2,
        "created_at": None,
        "old_key_prefix": "abc123456789",
    }

    with _patch_auth(), patch.object(user_routes, "get_runtime_settings_instance", return_value=rt):
        response = await regenerate_api_key(
            request=_make_request(),
            current_user=_make_current_user(role="internal"),
            op_store=op_store,
            db_logger=_make_db_logger(),
        )

    # The route names no key: the store picks the only one under the account lock.
    op_store.rotate_key.assert_awaited_once()
    assert op_store.rotate_key.call_args.args == ("user-123", None)
    op_store.create_key.assert_not_awaited()
    op_store.revoke_key.assert_not_awaited()
    rt.get_float.assert_not_awaited()
    assert response.old_key_prefix == "abc123456789"


@pytest.mark.asyncio
async def test_regenerate_api_key_with_several_keys_returns_409():
    from serving.exceptions import AmbiguousAPIKeyError

    op_store = _make_op_store(has_active_key=True)
    op_store.rotate_key.side_effect = AmbiguousAPIKeyError()

    with _patch_auth(), pytest.raises(HTTPException) as exc_info:
        await regenerate_api_key(
            request=_make_request(),
            current_user=_make_current_user(role="pro"),
            op_store=op_store,
            db_logger=_make_db_logger(),
        )

    assert exc_info.value.status_code == 409
    op_store.create_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_api_key_binds_the_named_project():
    """A named project is checked against the caller's account and bound to the key."""
    from serving.schemas_auth import APIKeyCreateRequest

    rt = _make_rt(250.0)
    op_store = _make_op_store(has_active_key=False)
    op_store.get_project.return_value = {"id": "proj_eval", "name": "Eval"}

    with _patch_auth(), patch.object(user_routes, "get_runtime_settings_instance", return_value=rt):
        response = await create_api_key(
            request=_make_request(),
            payload=APIKeyCreateRequest(name="eval-key", project_id="proj_eval"),
            current_user=_make_current_user(role="pro"),
            op_store=op_store,
            db_logger=_make_db_logger(),
        )

    op_store.get_project.assert_awaited_once_with("user-123", "proj_eval")
    op_store.get_default_project.assert_not_awaited()
    assert op_store.create_key.call_args.kwargs["project_id"] == "proj_eval"
    assert response.project_id == "proj_eval"
    assert response.project_name == "Eval"


@pytest.mark.asyncio
async def test_create_api_key_rejects_another_accounts_project():
    """A project the caller does not own is a 404, never a 403."""
    from serving.schemas_auth import APIKeyCreateRequest

    rt = _make_rt(250.0)
    op_store = _make_op_store(has_active_key=False)
    op_store.get_project.return_value = None

    with (
        _patch_auth(),
        patch.object(user_routes, "get_runtime_settings_instance", return_value=rt),
        pytest.raises(HTTPException) as exc_info,
    ):
        await create_api_key(
            request=_make_request(),
            payload=APIKeyCreateRequest(project_id="proj_someone_else"),
            current_user=_make_current_user(role="pro"),
            op_store=op_store,
            db_logger=_make_db_logger(),
        )

    assert exc_info.value.status_code == 404
    op_store.create_key.assert_not_awaited()
