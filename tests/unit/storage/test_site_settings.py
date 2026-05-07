"""Tests for OperationalStore site_settings methods."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.postgres_operational import PostgresOperationalStore


@pytest.fixture
def pg_conn():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


@pytest.fixture
def pg_pool(pg_conn):
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield pg_conn

    pool.acquire = _acquire
    return pool


@pytest.fixture
def store(pg_pool):
    return PostgresOperationalStore(pg_pool)


class TestGetSetting:
    async def test_returns_row_when_found(self, store, pg_conn):
        pg_conn.fetchrow.return_value = {
            "key": "signup_enabled",
            "value": "false",
            "value_type": "bool",
        }
        result = await store.get_setting("signup_enabled")
        assert result is not None
        assert result["key"] == "signup_enabled"
        assert result["value"] == "false"

    async def test_returns_none_when_not_found(self, store, pg_conn):
        pg_conn.fetchrow.return_value = None
        result = await store.get_setting("nonexistent")
        assert result is None


class TestSetSetting:
    async def test_calls_execute_with_params(self, store, pg_conn):
        await store.set_setting("signup_enabled", "false", "bool", "admin@test.com")
        pg_conn.execute.assert_awaited_once()
        call_args = pg_conn.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1:]
        assert "ON CONFLICT (key) DO UPDATE" in sql
        assert params[0] == "signup_enabled"
        assert params[1] == "false"
        assert params[2] == "bool"
        assert params[3] == "admin@test.com"


class TestListSettings:
    async def test_returns_all_rows(self, store, pg_conn):
        pg_conn.fetch.return_value = [
            {"key": "a", "value": "true", "value_type": "bool"},
            {"key": "b", "value": "5", "value_type": "int"},
        ]
        results = await store.list_settings()
        assert len(results) == 2

    async def test_returns_empty_list(self, store, pg_conn):
        pg_conn.fetch.return_value = []
        results = await store.list_settings()
        assert results == []
