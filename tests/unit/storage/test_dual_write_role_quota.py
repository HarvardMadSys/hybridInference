"""Tests for role-quota methods wired through DualWriteOperationalStore."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from serving.storage.dual_write import DualWriteOperationalStore


@pytest.mark.asyncio
async def test_apply_role_quota_fans_out_returns_primary_count():
    primary = AsyncMock()
    primary.apply_role_quota.return_value = 5
    shadow = AsyncMock()
    shadow.apply_role_quota.return_value = 5

    # Actual constructor: __init__(self, primary, shadow)
    store = DualWriteOperationalStore(primary=primary, shadow=shadow)
    n = await store.apply_role_quota("pro", Decimal("250.00"))

    assert n == 5
    primary.apply_role_quota.assert_awaited_once_with("pro", Decimal("250.00"))
    shadow.apply_role_quota.assert_awaited_once_with("pro", Decimal("250.00"))


@pytest.mark.asyncio
async def test_count_delegates_to_primary():
    primary = AsyncMock()
    primary.count_active_keys_for_role.return_value = (5, 4)
    shadow = AsyncMock()

    store = DualWriteOperationalStore(primary=primary, shadow=shadow)
    keys, users = await store.count_active_keys_for_role("pro")

    assert (keys, users) == (5, 4)
    shadow.count_active_keys_for_role.assert_not_called()
