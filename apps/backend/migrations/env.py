"""Alembic's environment: how a migration run reaches the database.

One connection, one run at a time. Before any revision runs, the connection
takes a session-level advisory lock, so a second run against the same database
waits for the first rather than migrating alongside it, and fails if that takes
too long. It waits by trying again (:func:`_lock_run`), holding no transaction
between tries: a revision building an index ``CONCURRENTLY`` waits for every
transaction older than the build, and a run blocked inside the lock would be one,
so the two would deadlock. Every statement a revision issues waits at most
:data:`DDL_LOCK_WAIT` for a table lock: a long reader such as a nightly
``pg_dump`` makes the revision fail, and roll back, instead of queueing an
exclusive lock that stalls every query behind it. Each revision commits in a
transaction of its own.

The database is the one ``alembic_config(url)`` names, the ``sqlalchemy.url``
option, or else the gateway's own ``DB_*`` settings. Offline (``--sql``) runs are
refused: the baseline reads the catalog to decide what to change.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from migrations import VERSION_TABLE, MigrationInProgress
from migrations.compat import ensure_compat_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

#: The advisory lock that serializes migration runs on one database.
RUN_LOCK_KEY = "hybridinference:schema-migrations"
#: How long a run waits for another to finish before it gives up.
RUN_LOCK_WAIT = "60s"
#: How often a run waiting for another tries the run lock again, in seconds.
RUN_LOCK_POLL_SEC = 0.2
#: How long a revision's statement waits for a table lock before it fails.
DDL_LOCK_WAIT = "3s"

config = context.config


def database_url() -> URL:
    """The database to migrate, as an asyncpg SQLAlchemy URL."""
    url = config.attributes.get("url") or config.get_main_option("sqlalchemy.url")
    if url:
        return make_url(url).set(drivername="postgresql+asyncpg")
    from serving.config.settings import DatabaseSettings

    settings = DatabaseSettings()
    return URL.create(
        "postgresql+asyncpg",
        username=settings.db_user,
        password=settings.db_password or None,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )


def _seconds(duration: str | float) -> float:
    """A wait given as PostgreSQL spells it (``"60s"``, ``"500ms"``), or a number, in seconds."""
    if isinstance(duration, int | float):
        return float(duration)
    text_value = duration.strip()
    if text_value.endswith("ms"):
        return float(text_value[:-2]) / 1000
    return float(text_value.removesuffix("s"))


def _lock_run(connection: Connection) -> None:
    """Take the run lock, waiting at most the run lock wait for another run to end.

    The wait is a series of ``pg_try_advisory_lock`` tries, each a statement of
    its own, with the transaction ended after every one. A run blocked inside
    ``pg_advisory_lock`` instead holds a transaction and its snapshot for the
    whole wait, and ``CREATE INDEX CONCURRENTLY`` in the run it waits for waits
    for every transaction older than the build to end: each would wait for the
    other, and PostgreSQL would end one of the two runs with a deadlock error.
    The lock is the session's, so ending a transaction keeps it.
    """
    # A caller may shorten the wait (``alembic_config().attributes["run_lock_wait"]``).
    wait = config.attributes.get("run_lock_wait", RUN_LOCK_WAIT)
    deadline = time.monotonic() + _seconds(wait)
    while True:
        taken = connection.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": RUN_LOCK_KEY}
        ).scalar()
        connection.rollback()
        if taken:
            break
        if time.monotonic() >= deadline:
            raise MigrationInProgress(
                f"another migration run has held this database for over {wait}; "
                "let it finish, then run this one again"
            )
        time.sleep(RUN_LOCK_POLL_SEC)
    connection.execute(text(f"SET lock_timeout = '{DDL_LOCK_WAIT}'"))


def do_run_migrations(connection: Connection) -> None:
    """Lock the run, then apply each pending revision in its own transaction."""
    _lock_run(connection)
    ensure_compat_table(connection)
    # Commit before configuring: a connection already inside a transaction
    # makes Alembic leave transactions to the caller, and every revision would
    # then share one.
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table=VERSION_TABLE,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open one connection for the run and close it, releasing the run lock."""
    engine = create_async_engine(database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit(
        "Offline (--sql) migrations are not supported: the baseline reads the catalog."
    )
asyncio.run(run_async_migrations())
