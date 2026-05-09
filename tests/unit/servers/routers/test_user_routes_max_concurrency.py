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
async def test_get_usage_includes_max_concurrency_when_no_key(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    helper_calls: list[str] = []

    async def _fake_helper(role, rt):
        helper_calls.append(role)
        return 99

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _fake_helper)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)

    current_user = {"user_id": "u1", "role": "free"}

    resp = await user_routes.get_usage(
        period="today",
        timezone_name="UTC",
        current_user=current_user,
        op_store=op_store,
        log_store=None,
    )
    assert resp.quota.has_key is False
    assert resp.quota.max_concurrency == 99
    assert helper_calls == ["free"]


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

    helper_calls: list[str] = []

    async def _fake_helper(role, rt):
        helper_calls.append(role)
        return 12

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _fake_helper)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)

    current_user = {"user_id": "u1", "role": "pro"}

    resp = await user_routes.get_usage(
        period="today",
        timezone_name="UTC",
        current_user=current_user,
        op_store=op_store,
        log_store=log_store,
    )
    assert resp.quota.has_key is True
    assert resp.quota.max_concurrency == 12
    assert helper_calls == ["pro"]


@pytest.mark.asyncio
async def test_get_usage_concurrency_when_runtime_unavailable(monkeypatch):
    from serving.servers.routers import user_routes

    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    def _raise():
        raise RuntimeError("not initialized")

    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", _raise)

    current_user = {"user_id": "u1", "role": "trial"}

    resp = await user_routes.get_usage(
        period="today",
        timezone_name="UTC",
        current_user=current_user,
        op_store=op_store,
        log_store=None,
    )

    from serving.servers.concurrency import _FALLBACK_LIMITS

    assert resp.quota.max_concurrency == _FALLBACK_LIMITS["trial"]
