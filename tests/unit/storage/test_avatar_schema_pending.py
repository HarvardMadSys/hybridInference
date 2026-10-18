"""The portrait column while an upgrade's users migration waits for its lock.

``avatar_data_url`` is added by a migration that, like the account columns,
can wait for the users table's lock (a ``pg_dump`` holds it) and land from the
background. Until it does, reading a portrait finds none, writing one answers
the upgrade-in-progress 503, and no route turns either into a 500.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import asyncpg
import pytest
from fastapi.testclient import TestClient

from serving.exceptions import AccountSchemaPending
from serving.servers.app import create_app
from serving.storage.postgres_operational import PostgresOperationalStore


class _MissingColumnConnection:
    async def fetchval(self, *args, **kwargs):
        raise asyncpg.UndefinedColumnError('column "avatar_data_url" does not exist')

    async def execute(self, *args, **kwargs):
        raise asyncpg.UndefinedColumnError('column "avatar_data_url" does not exist')


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield _MissingColumnConnection()


def _store() -> PostgresOperationalStore:
    store = PostgresOperationalStore.__new__(PostgresOperationalStore)
    store._pool = _Pool()
    return store


@pytest.mark.asyncio
async def test_no_portrait_is_read_before_the_column_exists() -> None:
    assert await _store().get_user_avatar("u1") is None


@pytest.mark.asyncio
async def test_storing_a_portrait_waits_for_the_upgrade() -> None:
    with pytest.raises(AccountSchemaPending):
        await _store().set_user_avatar("u1", "data:image/png;base64,AAAA")


def test_any_route_answers_the_upgrade_503_rather_than_a_500() -> None:
    app = create_app()

    @app.get("/__probe_account_schema_pending")
    async def probe():
        raise AccountSchemaPending()

    # No ``with``: the lifespan (bootstrap) stays out, only the mapping is tested.
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/__probe_account_schema_pending")

    assert response.status_code == 503
    assert AccountSchemaPending.MESSAGE in response.text
