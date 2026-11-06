"""The per-key cost counter never decides whether the account's counter is written."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = pytest.mark.asyncio


def _store(conn: AsyncMock) -> PostgresOperationalStore:
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return PostgresOperationalStore(pool)


async def test_a_failed_key_counter_write_leaves_the_account_increment_standing():
    conn = AsyncMock()
    conn.execute = AsyncMock(
        side_effect=[None, RuntimeError("relation does not exist"), None, None]
    )

    await _store(conn).increment_user_cost("u1", 0.5, day="2026-11-02", api_key_id=7)

    first, second = (call.args[0] for call in conn.execute.await_args_list[:2])
    assert first.startswith("INSERT INTO user_daily_cost")
    assert second.startswith("INSERT INTO api_key_daily_cost")


async def test_a_request_without_a_key_writes_only_the_account_counter():
    conn = AsyncMock()

    await _store(conn).increment_user_cost("u1", 0.5, day="2026-11-02")

    [call] = conn.execute.await_args_list
    assert call.args[0].startswith("INSERT INTO user_daily_cost")
