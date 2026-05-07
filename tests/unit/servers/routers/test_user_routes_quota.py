"""Tests for role-aware default-quota helper used at signup / regenerate."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from serving.servers.routers.user_routes import get_default_daily_quota_for_role


@pytest.mark.asyncio
async def test_picks_role_specific_runtime_setting():
    rt = AsyncMock()
    rt.get_float.return_value = 250.0
    quota = await get_default_daily_quota_for_role("pro", rt)
    rt.get_float.assert_awaited_once_with("user_daily_quota_pro")
    assert quota == Decimal("250.0")


@pytest.mark.asyncio
async def test_unknown_role_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "77.00")
    rt = AsyncMock()
    quota = await get_default_daily_quota_for_role("ghost", rt)
    rt.get_float.assert_not_awaited()
    assert quota == Decimal("77.00")


@pytest.mark.asyncio
async def test_runtime_settings_none_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", "55.50")
    quota = await get_default_daily_quota_for_role("free", None)
    assert quota == Decimal("55.50")


@pytest.mark.asyncio
async def test_default_when_no_env_no_rt():
    monkeypatch_pytest = pytest.MonkeyPatch()
    monkeypatch_pytest.delenv("SIGNUP_DEFAULT_DAILY_QUOTA_USD", raising=False)
    try:
        quota = await get_default_daily_quota_for_role("free", None)
        assert quota == Decimal("100.00")
    finally:
        monkeypatch_pytest.undo()
