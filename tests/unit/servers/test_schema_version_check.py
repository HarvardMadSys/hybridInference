"""Tests for the ``_verify_schema_version`` boot guard.

These tests mock out asyncpg's pool / connection objects so they can run
without a live Postgres instance. The full-stack roundtrip is covered by
``tests/integration/storage/test_migrations.py`` (which requires the
``dbtest`` marker).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from serving.servers.bootstrap import _verify_schema_version


def _make_pool(conn: MagicMock) -> MagicMock:
    """Build a MagicMock pool whose ``acquire()`` async-context yields *conn*."""
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool


async def test_passes_when_db_matches_expected(monkeypatch):
    """Should return without raising when DB version matches the pin."""
    import serving.storage._expected_alembic_version as version_mod

    monkeypatch.setattr(version_mod, "EXPECTED_ALEMBIC_VERSION", "0001_baseline")

    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="0001_baseline")
    pool = _make_pool(conn)
    settings = MagicMock(db_backend="postgres")

    # Should not raise.
    await _verify_schema_version(pool, settings)
    conn.fetchval.assert_awaited_once()


async def test_raises_on_version_mismatch(monkeypatch):
    """A DB at the wrong version must hard-fail boot."""
    import serving.storage._expected_alembic_version as version_mod

    monkeypatch.setattr(version_mod, "EXPECTED_ALEMBIC_VERSION", "0002_add_foo")

    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="0001_baseline")
    pool = _make_pool(conn)
    settings = MagicMock(db_backend="postgres")

    with pytest.raises(RuntimeError, match="Schema version mismatch"):
        await _verify_schema_version(pool, settings)


async def test_raises_when_alembic_version_table_missing():
    """Missing ``alembic_version`` table → operator hasn't run stamp/upgrade."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(
        side_effect=asyncpg.UndefinedTableError("relation does not exist")
    )
    pool = _make_pool(conn)
    settings = MagicMock(db_backend="postgres")

    with pytest.raises(RuntimeError, match="alembic_version table missing"):
        await _verify_schema_version(pool, settings)


async def test_skips_when_backend_not_postgres():
    """D1 backend does not participate in the Alembic chain."""
    pool = MagicMock()
    settings = MagicMock(db_backend="d1")

    await _verify_schema_version(pool, settings)
    pool.acquire.assert_not_called()
