"""The hard-delete purge of batches works before the batch tables exist.

A store whose schema migration is still pending at boot is purged through all
the same; on a database that never had the tables there is nothing to purge,
which must not fail the erasure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from serving.storage.batch_store import BatchStore


def _pool(conn: MagicMock) -> MagicMock:
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


@pytest.mark.asyncio
async def test_purge_reports_the_batches_it_deleted():
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="DELETE 3")

    assert await BatchStore(_pool(conn)).delete_user_batches("u1") == 3
    assert conn.execute.await_args.args[1] == "u1"


@pytest.mark.asyncio
async def test_purge_finds_nothing_where_the_tables_do_not_exist_yet():
    conn = MagicMock()
    conn.execute = AsyncMock(
        side_effect=asyncpg.UndefinedTableError('relation "batch_jobs" does not exist')
    )

    assert await BatchStore(_pool(conn)).delete_user_batches("u1") == 0
