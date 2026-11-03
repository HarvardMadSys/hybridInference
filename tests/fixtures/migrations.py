"""Run the schema migrations against a test database, and make scratch databases for it.

The migrations need a database of their own whenever a test compares what they
build: :func:`scratch_database` creates one on the test server (its name marks it
as a test database) and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from tests.fixtures import postgres

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def url(database: str) -> str:
    """A SQLAlchemy URL for *database* on the test server."""
    db = postgres.target()
    return (
        f"postgresql+asyncpg://{quote(db.user, safe='')}:{quote(db.password, safe='')}"
        f"@{db.host}:{db.port}/{database}"
    )


def upgrade_sync(database: str, revision: str = "head") -> None:
    """Apply the migrations to *database* up to *revision*, from synchronous code."""
    from alembic import command

    from migrations import alembic_config

    command.upgrade(alembic_config(url(database)), revision)


async def upgrade(database: str, revision: str = "head") -> None:
    """Apply the migrations to *database* up to *revision*.

    Alembic runs its own event loop, so it gets a thread of its own.
    """
    await asyncio.to_thread(upgrade_sync, database, revision)


@contextlib.asynccontextmanager
async def connected(database: str) -> AsyncIterator[Any]:
    """An asyncpg connection to *database* on the test server."""
    conn = await postgres.connect(database=database)
    try:
        yield conn
    finally:
        await conn.close()


@contextlib.asynccontextmanager
async def scratch_database() -> AsyncIterator[str]:
    """Create an empty database for one test; drop it afterwards."""
    name = f"migration_test_{uuid.uuid4().hex[:12]}"
    await postgres.ensure_database(name)
    try:
        yield name
    finally:
        admin = await postgres.connect_maintenance()
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()
