"""/user/usage reports the quota and concurrency cap the gate enforces, user tags included."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from serving.servers.routers import user_routes
from serving.user_tags import TAG_MAX_CONCURRENT_KEY, TAG_QUOTA_DAILY_KEY, TAG_QUOTA_MONTHLY_KEY

pytestmark = pytest.mark.asyncio


async def _usage(monkeypatch, current_user, key_row):
    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = key_row
    op_store.get_user_cost_today.return_value = 1.5
    log_store = AsyncMock()
    log_store.get_user_usage_detail.return_value = {}

    async def _cap(role, rt, *, is_admin=False):
        return 8

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _cap)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)
    resp = await user_routes.get_usage(
        period="today", current_user=current_user, op_store=op_store, log_store=log_store
    )
    return resp.quota


async def test_a_tag_quota_below_the_account_is_the_one_reported(monkeypatch):
    quota = await _usage(
        monkeypatch,
        {
            "user_id": "u1",
            "role": "free",
            TAG_QUOTA_DAILY_KEY: Decimal("5"),
            TAG_QUOTA_MONTHLY_KEY: Decimal("20"),
            TAG_MAX_CONCURRENT_KEY: 2,
        },
        {"quota_daily_cost_usd": Decimal("100")},
    )
    assert quota.daily_limit_usd == 5.0
    assert quota.remaining_today_usd == 3.5
    assert quota.monthly_limit_usd == 20.0
    assert quota.max_concurrency == 2


async def test_without_tags_the_account_quota_is_reported_as_before(monkeypatch):
    quota = await _usage(
        monkeypatch,
        {"user_id": "u1", "role": "free"},
        {"quota_daily_cost_usd": Decimal("100"), "quota_monthly_cost_usd": Decimal("40")},
    )
    assert quota.daily_limit_usd == 100.0
    assert quota.remaining_today_usd == 98.5
    assert quota.monthly_limit_usd is None
    assert quota.max_concurrency == 8


async def test_a_tag_quota_above_the_account_changes_nothing(monkeypatch):
    quota = await _usage(
        monkeypatch,
        {"user_id": "u1", "role": "free", TAG_QUOTA_DAILY_KEY: Decimal("500")},
        {"quota_daily_cost_usd": None},
    )
    # An unset key quota is the 1000 USD default, which 500 tightens.
    assert quota.daily_limit_usd == 500.0
    quota = await _usage(
        monkeypatch,
        {"user_id": "u1", "role": "free", TAG_QUOTA_DAILY_KEY: Decimal("500")},
        {"quota_daily_cost_usd": Decimal("100")},
    )
    assert quota.daily_limit_usd == 100.0
