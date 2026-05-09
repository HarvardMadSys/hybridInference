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
