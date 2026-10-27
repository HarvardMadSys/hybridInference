"""Tests for the per-user max-concurrency resolver and /user/usage exposure."""

from unittest.mock import AsyncMock

import pytest

from serving.servers.routers.user_routes import get_user_concurrency_for_role


@pytest.mark.asyncio
async def test_helper_reads_runtime_setting_for_role():
    rt = AsyncMock()
    rt.get_int.return_value = 7
    cap = await get_user_concurrency_for_role("pro", rt)
    rt.get_int.assert_awaited_once_with("user_concurrency_pro")
    assert cap == 7


@pytest.mark.asyncio
async def test_helper_unknown_role_falls_back_to_free_constant():
    rt = AsyncMock()
    cap = await get_user_concurrency_for_role("ghost", rt)
    rt.get_int.assert_not_awaited()

    from serving.servers.concurrency import _FALLBACK_LIMITS

    assert cap == _FALLBACK_LIMITS["free"]


@pytest.mark.asyncio
async def test_helper_runtime_settings_none_falls_back_to_constant():
    from serving.servers.concurrency import _FALLBACK_LIMITS

    cap = await get_user_concurrency_for_role("pro", None)
    assert cap == _FALLBACK_LIMITS["pro"]


@pytest.mark.asyncio
async def test_helper_lowercases_role():
    rt = AsyncMock()
    rt.get_int.return_value = 4
    cap = await get_user_concurrency_for_role("Pro", rt)
    rt.get_int.assert_awaited_once_with("user_concurrency_pro")
    assert cap == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_role", [None, ""])
async def test_helper_treats_missing_role_as_free(empty_role):
    rt = AsyncMock()
    rt.get_int.return_value = 9
    cap = await get_user_concurrency_for_role(empty_role, rt)
    rt.get_int.assert_awaited_once_with("user_concurrency_free")
    assert cap == 9


@pytest.mark.asyncio
async def test_helper_is_admin_overrides_role():
    rt = AsyncMock()
    rt.get_int.return_value = 42
    cap = await get_user_concurrency_for_role("free", rt, is_admin=True)
    rt.get_int.assert_awaited_once_with("user_concurrency_admin")
    assert cap == 42


@pytest.mark.asyncio
async def test_helper_is_admin_uses_admin_fallback_when_runtime_unavailable():
    from serving.servers.concurrency import _FALLBACK_LIMITS

    cap = await get_user_concurrency_for_role("free", None, is_admin=True)
    assert cap == _FALLBACK_LIMITS["admin"]


@pytest.mark.asyncio
async def test_get_usage_includes_max_concurrency_when_no_key(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    helper_calls: list[tuple[str, bool]] = []

    async def _fake_helper(role, rt, *, is_admin=False):
        helper_calls.append((role, is_admin))
        return 99

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _fake_helper)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)

    current_user = {"user_id": "u1", "role": "free"}

    resp = await user_routes.get_usage(
        period="today",
        current_user=current_user,
        op_store=op_store,
        log_store=None,
    )
    assert resp.quota.has_key is False
    assert resp.quota.max_concurrency == 99
    assert helper_calls == [("free", False)]


@pytest.mark.asyncio
async def test_get_usage_includes_max_concurrency_when_has_key(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = {
        "quota_daily_cost_usd": 5.0,
    }
    op_store.get_user_cost_today.return_value = 0.0

    log_store = AsyncMock()
    log_store.get_user_usage_detail.return_value = {
        "today": {"cost_usd": 0.0, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0},
        "month": {"cost_usd": 0.0, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0},
    }

    helper_calls: list[tuple[str, bool]] = []

    async def _fake_helper(role, rt, *, is_admin=False):
        helper_calls.append((role, is_admin))
        return 12

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _fake_helper)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)

    current_user = {"user_id": "u1", "role": "pro"}

    resp = await user_routes.get_usage(
        period="today",
        current_user=current_user,
        op_store=op_store,
        log_store=log_store,
    )
    assert resp.quota.has_key is True
    assert resp.quota.max_concurrency == 12
    assert helper_calls == [("pro", False)]


@pytest.mark.asyncio
async def test_get_usage_concurrency_when_runtime_unavailable(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    def _raise():
        raise RuntimeError("not initialized")

    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", _raise)

    current_user = {"user_id": "u1", "role": "free"}

    resp = await user_routes.get_usage(
        period="today",
        current_user=current_user,
        op_store=op_store,
        log_store=None,
    )

    from serving.servers.concurrency import _FALLBACK_LIMITS

    assert resp.quota.max_concurrency == _FALLBACK_LIMITS["free"]


@pytest.mark.asyncio
async def test_get_usage_propagates_is_admin_to_helper(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    helper_calls: list[tuple[str, bool]] = []

    async def _fake_helper(role, rt, *, is_admin=False):
        helper_calls.append((role, is_admin))
        return 50

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _fake_helper)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)

    current_user = {"user_id": "u1", "role": "free", "is_admin": True}

    resp = await user_routes.get_usage(
        period="today",
        current_user=current_user,
        op_store=op_store,
        log_store=None,
    )
    assert resp.quota.max_concurrency == 50
    assert helper_calls == [("free", True)]


@pytest.mark.asyncio
async def test_get_usage_shows_per_user_override(monkeypatch):
    """An admin-set per-user cap is what the limiter enforces, so it is shown."""
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    async def _fake_helper(role, rt, *, is_admin=False):
        raise AssertionError("role cap must not be read when an override is set")

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _fake_helper)

    current_user = {"user_id": "u1", "role": "free", "max_concurrent_requests": 256}

    resp = await user_routes.get_usage(
        period="today",
        current_user=current_user,
        op_store=op_store,
        log_store=None,
    )
    assert resp.quota.max_concurrency == 256


@pytest.mark.asyncio
async def test_get_usage_null_override_uses_role_cap(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    async def _fake_helper(role, rt, *, is_admin=False):
        return 4

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _fake_helper)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)

    current_user = {"user_id": "u1", "role": "free", "max_concurrent_requests": None}

    resp = await user_routes.get_usage(
        period="today",
        current_user=current_user,
        op_store=op_store,
        log_store=None,
    )
    assert resp.quota.max_concurrency == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, 256])
async def test_dashboard_user_context_carries_override(override, monkeypatch):
    """``get_current_user`` hands ``/user/usage`` the per-user override."""
    from serving.config.settings import get_settings
    from serving.servers.deps import get_current_user
    from serving.utils.jwt import create_access_token

    monkeypatch.setattr(
        get_settings(), "jwt_secret_key", "test-secret-for-usage-override-long-enough"
    )

    op_store = AsyncMock()
    op_store.get_user_by_id.return_value = {
        "id": "u1",
        "email": "u1@example.com",
        "role": "free",
        "status": "active",
        "email_verified": True,
        "max_concurrent_requests": override,
    }
    token, _ = create_access_token(user_id="u1", email="u1@example.com")

    user = await get_current_user("Bearer " + token, op_store)
    assert user["max_concurrent_requests"] == override
