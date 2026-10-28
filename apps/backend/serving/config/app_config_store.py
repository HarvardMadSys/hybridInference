"""Postgres storage for the database-backed configuration.

One table, ``app_config``, holding each setting in the string form its
environment variable takes (``true``, ``8``, ``a,b,c``), so importing the
environment is lossless and one coercion path serves both sources. It is
created here rather than with the logging or operational schemas because the
configuration is read before either of those stores exists.

A row whose ``source`` is :data:`RESET_SOURCE` stores no value. It records that
an administrator reset the setting, which follows the environment, then its
default, from then on: :meth:`AppConfigStore.fetch_all` leaves it out unless
asked for it, and its key blocks the boot-time import, which would otherwise
store the environment value again and stop the setting following later changes
to the environment. It keeps the setting's ``secret`` flag, which only ever
goes from false to true (:meth:`AppConfigStore.mark_secret`).

Every write to the table advances the source version in
``config_source_revisions`` (:mod:`serving.config.revisions`), from a trigger
created with the table. :meth:`AppConfigStore.transaction` locks that version
row after the write lock and can refuse a stale ``If-Match``;
:meth:`AppConfigStore.fetch_snapshot` reads the rows and the version together.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

from serving.config import revisions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from datetime import datetime

    from serving.config.settings import DatabaseSettings, Settings

#: Connect and statement deadlines for the short-lived boot connection. The
#: pool's 60 s defaults would let one unreachable host hold startup for minutes
#: across the retries.
CONNECT_TIMEOUT_SECONDS = 10.0
COMMAND_TIMEOUT_SECONDS = 30.0

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS app_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    secret      BOOLEAN NOT NULL DEFAULT FALSE,
    source      TEXT NOT NULL DEFAULT 'admin',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  TEXT
)
"""

#: The ``source`` of a row that marks a reset instead of storing a value.
RESET_SOURCE = "reset"

SELECT_ALL_WITH_RESET_SQL = (
    "SELECT key, value, secret, source, updated_at, updated_by FROM app_config"
)

SELECT_ALL_SQL = f"{SELECT_ALL_WITH_RESET_SQL} WHERE source <> '{RESET_SOURCE}'"

# One-way: a secret is never made public again. A reset row is marked too, so
# the setting stays secret while it follows the environment. The value and the
# updated_* columns describe the value, which does not change.
MARK_SECRET_SQL = """
UPDATE app_config SET secret = TRUE
WHERE key = ANY($1::text[]) AND NOT secret
RETURNING key
"""

# Rows that do not exist yet, and only those: several workers booting at once
# all try the same import, and an administrator's value must never be
# overwritten by an environment one. A reset row conflicts too, which is what
# keeps a reset setting following the environment. RETURNING names the rows
# this call added.
INSERT_MISSING_SQL = """
INSERT INTO app_config (key, value, secret, source, updated_by)
SELECT * FROM unnest($1::text[], $2::text[], $3::boolean[], $4::text[], $5::text[])
ON CONFLICT (key) DO NOTHING
RETURNING key
"""

# The same, except that a reset row is replaced: for a generated secret, which
# must exist once neither the table nor the environment holds one. The row lock
# makes a concurrent worker re-check the condition against the winner's row.
INSERT_MISSING_OR_RESET_SQL = f"""
INSERT INTO app_config (key, value, secret, source, updated_by)
SELECT * FROM unnest($1::text[], $2::text[], $3::boolean[], $4::text[], $5::text[])
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    secret = app_config.secret OR EXCLUDED.secret,
    source = EXCLUDED.source,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by
WHERE app_config.source = '{RESET_SOURCE}'
RETURNING key
"""

# Neither a write nor a reset clears the secret flag: it is one-way.
UPSERT_SQL = """
INSERT INTO app_config (key, value, secret, source, updated_at, updated_by)
VALUES ($1, $2, $3, $4, NOW(), $5)
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    secret = app_config.secret OR EXCLUDED.secret,
    source = EXCLUDED.source,
    updated_at = EXCLUDED.updated_at,
    updated_by = EXCLUDED.updated_by
"""

RESET_SQL = f"""
INSERT INTO app_config (key, value, secret, source, updated_at, updated_by)
VALUES ($1, '', $2, '{RESET_SOURCE}', NOW(), $3)
ON CONFLICT (key) DO UPDATE SET
    value = '',
    secret = app_config.secret OR EXCLUDED.secret,
    source = '{RESET_SOURCE}',
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by
"""

DELETE_SQL = "DELETE FROM app_config WHERE key = $1"

# Shared by boot import, API updates/resets and the recovery CLI. The lock is
# database-wide, so separate workers also validate against the preceding write.
CONFIG_WRITE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hybridinference:app-config'))"


@dataclass(frozen=True)
class ConfigRow:
    """One stored setting."""

    key: str
    value: str
    secret: bool = False
    #: ``admin``, ``setup``, ``env_import`` or ``generated``.
    source: str = "admin"
    updated_at: datetime | None = None
    updated_by: str | None = None


class AppConfigStore:
    """Reads and writes ``app_config`` over an asyncpg pool or one connection."""

    def __init__(
        self,
        db: asyncpg.Pool | asyncpg.Connection | Any,
        *,
        locked_version: revisions.SourceVersion | None = None,
    ) -> None:
        self._db = db
        #: The version this store's transaction locked, for a store handed out
        #: by :meth:`transaction`; ``None`` otherwise.
        self.locked_version = locked_version

    @contextlib.asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        if hasattr(self._db, "acquire"):
            async with self._db.acquire() as conn:
                yield conn
        else:
            yield self._db

    def connection(self) -> contextlib.AbstractAsyncContextManager[Any]:
        """Use the store's connection: the one a transaction holds, or one from the pool."""
        return self._connection()

    async def ensure_schema(self) -> None:
        """Create the table, its version row and trigger, and the instance table.

        Idempotent. The version starts at revision 0 with no active group.
        """
        async with self._connection() as conn:
            await revisions.ensure_schema(conn, create_source_table=CREATE_TABLE_SQL)

    @contextlib.asynccontextmanager
    async def transaction(
        self, *, expected: revisions.SourceVersion | None = None
    ) -> AsyncIterator[AppConfigStore]:
        """Serialize a read/validate/write operation on one database connection.

        Acquire the write lock, then the source's version row, before reading.
        READ COMMITTED then sees the previous writer's commit, even when this
        transaction waited for it, and the row lock also holds off a writer
        that bypasses the write lock (plain SQL), whose trigger needs the row.
        Release both the transaction and the locks before reloading the
        process's cache.

        Args:
            expected: Refuse the write unless the source is still at this
                version (an ``If-Match``). ``None`` keeps last-write-wins.

        Raises:
            revisions.VersionConflict: *expected* is no longer current.
        """
        async with self._connection() as conn, conn.transaction(isolation="read_committed"):
            await conn.execute(CONFIG_WRITE_LOCK_SQL)
            version, _ = await revisions.lock_version(conn)
            revisions.check_expected(expected, version)
            yield AppConfigStore(conn, locked_version=version)

    async def current_version(self) -> revisions.SourceVersion | None:
        """Return the source version as this connection sees it now.

        Inside :meth:`transaction`, after a write, it includes that write's
        advance.
        """
        async with self._connection() as conn:
            version, _ = await revisions.read_version(conn)
        return version

    async def fetch_snapshot(self) -> revisions.SourceSnapshot:
        """Read every row, reset rows included, and the version in one snapshot.

        A short read-only REPEATABLE READ transaction, so the version names
        exactly the rows returned, an empty table included. On a connection
        already inside a transaction (one :meth:`transaction` handed out), the
        reads join it: the locks it holds keep both consistent.
        """
        async with self._connection() as conn:
            if conn.is_in_transaction():
                return await self._snapshot(conn)
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                return await self._snapshot(conn)

    @staticmethod
    async def _snapshot(conn: Any) -> revisions.SourceSnapshot:
        version, contracts = await revisions.read_version(conn)
        records = await conn.fetch(SELECT_ALL_WITH_RESET_SQL)
        return revisions.SourceSnapshot(
            source=revisions.APP_CONFIG_SOURCE,
            version=version,
            contracts=contracts,
            rows={record["key"]: _row(record) for record in records},
        )

    async def fetch_all(self, *, include_reset: bool = False) -> dict[str, ConfigRow]:
        """Return every stored setting, keyed by name.

        Args:
            include_reset: Also return the reset rows (``source`` is
                :data:`RESET_SOURCE`), which store no value but keep the
                setting's secret flag. Left out by default.
        """
        async with self._connection() as conn:
            records = await conn.fetch(
                SELECT_ALL_WITH_RESET_SQL if include_reset else SELECT_ALL_SQL
            )
        return {record["key"]: _row(record) for record in records}

    async def insert_missing(
        self, rows: Sequence[ConfigRow], *, replace_reset: bool = False
    ) -> list[str]:
        """Insert the rows whose keys are not stored yet; return the keys added.

        Args:
            rows: The rows to add.
            replace_reset: Also replace a reset row. Without it, a key an
                administrator reset is left alone.
        """
        if not rows:
            return []
        async with self._connection() as conn:
            records = await conn.fetch(
                INSERT_MISSING_OR_RESET_SQL if replace_reset else INSERT_MISSING_SQL,
                [row.key for row in rows],
                [row.value for row in rows],
                [row.secret for row in rows],
                [row.source for row in rows],
                [row.updated_by for row in rows],
            )
        return [record["key"] for record in records]

    async def write(self, rows: Sequence[ConfigRow]) -> None:
        """Insert or replace *rows* in one transaction."""
        if not rows:
            return
        async with self._connection() as conn, conn.transaction():
            await conn.executemany(
                UPSERT_SQL,
                [(row.key, row.value, row.secret, row.source, row.updated_by) for row in rows],
            )

    async def mark_secret(self, keys: Sequence[str]) -> list[str]:
        """Mark the stored rows of *keys* secret, reset rows included; return those changed.

        Never the other way: there is no call that clears the flag.
        """
        if not keys:
            return []
        async with self._connection() as conn:
            records = await conn.fetch(MARK_SECRET_SQL, list(keys))
        return [record["key"] for record in records]

    async def reset(self, key: str, *, secret: bool, updated_by: str) -> None:
        """Replace *key*'s stored value with a reset row, so it follows the environment."""
        async with self._connection() as conn:
            await conn.execute(RESET_SQL, key, secret, updated_by)

    async def delete(self, key: str) -> bool:
        """Remove one stored setting outright; return whether a row existed."""
        async with self._connection() as conn:
            status = await conn.execute(DELETE_SQL, key)
        return status.rsplit(" ", 1)[-1] != "0"

    async def api_keys_exist(self) -> bool:
        """Return whether any user API key has been issued."""
        async with self._connection() as conn:
            if await conn.fetchval("SELECT to_regclass('api_keys')") is None:
                return False
            return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM api_keys)"))


def _row(record: Any) -> ConfigRow:
    return ConfigRow(
        key=record["key"],
        value=record["value"],
        secret=record["secret"],
        source=record["source"],
        updated_at=record["updated_at"],
        updated_by=record["updated_by"],
    )


async def connect(settings: Settings | DatabaseSettings) -> asyncpg.Connection:
    """Open the short-lived connection the boot-time configuration load uses.

    Reads only the ``db_*`` fields, so a command-line tool can pass
    :class:`~serving.config.settings.DatabaseSettings`.
    """
    return await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        timeout=CONNECT_TIMEOUT_SECONDS,
        command_timeout=COMMAND_TIMEOUT_SECONDS,
    )
