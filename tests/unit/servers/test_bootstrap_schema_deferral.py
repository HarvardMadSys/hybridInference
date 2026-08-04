"""Startup must survive a schema migration it cannot get a lock for.

The failure this guards against: a deploy landing during the nightly ``pg_dump``
could not take ``ACCESS EXCLUSIVE`` on ``api_logs``, so bootstrap threw away a
perfectly good connection pool, ``/health`` latched to 503
``database_unavailable_at_startup``, and compose refused to start the services
gated on backend health — a total site outage caused by one blocked ``ALTER``.
"""

from __future__ import annotations

import asyncio

import pytest

from serving.servers.bootstrap import (
    _describe_exc,
    _schedule_deferred_schema_migration,
)
from serving.storage.log_schema import SchemaLockUnavailable


def test_describe_exc_keeps_the_type_when_the_message_is_empty():
    """The bug that hid the outage: TimeoutError stringifies to nothing.

    ``f"{asyncio.TimeoutError()}"`` is ``""``, so the deploy log read
    ``Database initialization failed (attempt 1/3): .`` and named no cause.
    """
    assert str(asyncio.TimeoutError()) == ""
    described = _describe_exc(asyncio.TimeoutError())
    assert described.strip(), "an empty rendering is exactly the bug"
    assert "TimeoutError" in described


def test_describe_exc_keeps_the_message_when_there_is_one():
    assert "boom" in _describe_exc(ValueError("boom"))
    assert "ValueError" in _describe_exc(ValueError("boom"))


class _Logger:
    """Minimal DatabaseLogger stand-in recording ensure_schema attempts."""

    def __init__(self, failures: int) -> None:
        self._remaining = failures
        self.attempts = 0

    async def ensure_schema(self) -> None:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise SchemaLockUnavailable("still locked")


@pytest.mark.asyncio
async def test_deferred_migration_retries_until_the_lock_clears(monkeypatch):
    """The migration lands on its own once the lock holder goes away."""
    monkeypatch.setattr("serving.servers.bootstrap._SCHEMA_RETRY_INITIAL_DELAY", 0)
    monkeypatch.setattr("serving.servers.bootstrap._SCHEMA_RETRY_MAX_DELAY", 0)
    db_logger = _Logger(failures=3)

    _schedule_deferred_schema_migration(db_logger)
    for _ in range(50):
        await asyncio.sleep(0)
        if db_logger.attempts >= 4:
            break

    assert db_logger.attempts == 4, "expected 3 lock failures then one success"


@pytest.mark.asyncio
async def test_deferred_migration_gives_up_rather_than_retrying_forever(monkeypatch):
    """The retry task is bounded, so it cannot outlive the problem silently."""
    monkeypatch.setattr("serving.servers.bootstrap._SCHEMA_RETRY_INITIAL_DELAY", 0)
    monkeypatch.setattr("serving.servers.bootstrap._SCHEMA_RETRY_MAX_DELAY", 0)
    monkeypatch.setattr("serving.servers.bootstrap._SCHEMA_RETRY_MAX_ATTEMPTS", 3)
    db_logger = _Logger(failures=99)

    _schedule_deferred_schema_migration(db_logger)
    for _ in range(50):
        await asyncio.sleep(0)
        if db_logger.attempts >= 3:
            break
    await asyncio.sleep(0)

    assert db_logger.attempts == 3


@pytest.mark.asyncio
async def test_deferred_migration_stops_on_a_non_lock_error(monkeypatch):
    """A genuine schema error is reported once, not retried for hours."""
    monkeypatch.setattr("serving.servers.bootstrap._SCHEMA_RETRY_INITIAL_DELAY", 0)
    monkeypatch.setattr("serving.servers.bootstrap._SCHEMA_RETRY_MAX_DELAY", 0)

    class _Broken:
        attempts = 0

        async def ensure_schema(self) -> None:
            _Broken.attempts += 1
            raise ValueError("column definition is wrong")

    _schedule_deferred_schema_migration(_Broken())
    for _ in range(50):
        await asyncio.sleep(0)
        if _Broken.attempts:
            break
    await asyncio.sleep(0)

    assert _Broken.attempts == 1
