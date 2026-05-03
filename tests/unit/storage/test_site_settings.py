"""Tests for OperationalStore site_settings methods (D1 mock variant)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.d1_client import D1Result
from serving.storage.d1_operational import D1OperationalStore


@pytest.fixture
def d1_client():
    client = MagicMock()
    client.query = AsyncMock(return_value=D1Result())
    client.execute = AsyncMock(return_value=D1Result())
    client.batch = AsyncMock(return_value=[])
    client.health_check = AsyncMock(return_value=True)
    client.close = AsyncMock()
    return client


@pytest.fixture
def store(d1_client):
    return D1OperationalStore(d1_client)


class TestGetSetting:
    async def test_returns_row_when_found(self, store, d1_client):
        d1_client.query.return_value = D1Result(
            rows=[{"key": "signup_enabled", "value": "false", "value_type": "bool"}]
        )
        result = await store.get_setting("signup_enabled")
        assert result is not None
        assert result["key"] == "signup_enabled"
        assert result["value"] == "false"

    async def test_returns_none_when_not_found(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[])
        result = await store.get_setting("nonexistent")
        assert result is None


class TestSetSetting:
    async def test_calls_execute_with_params(self, store, d1_client):
        await store.set_setting("signup_enabled", "false", "bool", "admin@test.com")
        d1_client.execute.assert_awaited_once()
        call_args = d1_client.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "INSERT OR REPLACE" in sql
        assert params[0] == "signup_enabled"
        assert params[1] == "false"
        assert params[2] == "bool"
        assert params[4] == "admin@test.com"


class TestListSettings:
    async def test_returns_all_rows(self, store, d1_client):
        d1_client.query.return_value = D1Result(
            rows=[
                {"key": "a", "value": "true", "value_type": "bool"},
                {"key": "b", "value": "5", "value_type": "int"},
            ]
        )
        results = await store.list_settings()
        assert len(results) == 2

    async def test_returns_empty_list(self, store, d1_client):
        d1_client.query.return_value = D1Result(rows=[])
        results = await store.list_settings()
        assert results == []
