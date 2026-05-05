"""Unit tests for D1 role-quota helpers (mocks the D1Client)."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.d1_client import D1Result
from serving.storage.d1_operational import D1OperationalStore


@pytest.fixture
def d1_client() -> MagicMock:
    """Create a mock D1Client."""
    client = MagicMock()
    client.query = AsyncMock(return_value=D1Result())
    client.execute = AsyncMock(return_value=D1Result())
    return client


@pytest.fixture
def store(d1_client: MagicMock) -> D1OperationalStore:
    """Create a D1OperationalStore with mock client."""
    return D1OperationalStore(d1_client)


@pytest.mark.asyncio
async def test_count_active_keys_for_role(store, d1_client):
    d1_client.query.return_value = D1Result(rows=[{"keys": 5, "users": 4}])
    keys, users = await store.count_active_keys_for_role("pro")
    assert (keys, users) == (5, 4)
    sql, params = d1_client.query.call_args[0][:2]
    assert "WHERE k.status = 'active'" in sql
    assert "u.role = ?" in sql
    assert params == ["pro"]


@pytest.mark.asyncio
async def test_count_active_keys_for_role_empty(store, d1_client):
    d1_client.query.return_value = D1Result(rows=[])
    keys, users = await store.count_active_keys_for_role("free")
    assert (keys, users) == (0, 0)


@pytest.mark.asyncio
async def test_apply_role_quota_returns_changes(store, d1_client):
    d1_client.execute.return_value = D1Result(changes=7)
    n = await store.apply_role_quota("pro", Decimal("250.00"))
    assert n == 7
    sql, params = d1_client.execute.call_args[0][:2]
    assert sql.strip().startswith("UPDATE api_keys")
    assert params == [250.0, "pro"]


@pytest.mark.asyncio
async def test_apply_role_quota_no_changes(store, d1_client):
    d1_client.execute.return_value = D1Result(changes=0)
    n = await store.apply_role_quota("free", Decimal("100.00"))
    assert n == 0
