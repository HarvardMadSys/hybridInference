"""Postgres storage for the database-backed configuration.

One table, ``app_config``, holding each setting in the string form its
environment variable takes (``true``, ``8``, ``a,b,c``), so importing the
environment is lossless and one coercion path serves both sources. It is
created here rather than with the logging or operational schemas because the
configuration is read before either of those stores exists.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from datetime import datetime

    from serving.config.settings import Settings

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

SELECT_ALL_SQL = "SELECT key, value, secret, source, updated_at, updated_by FROM app_config"

# Rows that do not exist yet, and only those: several workers booting at once
# all try the same import, and an administrator's value must never be
# overwritten by an environment one. RETURNING names the rows this call added.
INSERT_MISSING_SQL = """
INSERT INTO app_config (key, value, secret, source, updated_by)
SELECT * FROM unnest($1::text[], $2::text[], $3::boolean[], $4::text[], $5::text[])
ON CONFLICT (key) DO NOTHING
RETURNING key
"""

UPSERT_SQL = """
INSERT INTO app_config (key, value, secret, source, updated_at, updated_by)
VALUES ($1, $2, $3, $4, NOW(), $5)
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    secret = EXCLUDED.secret,
    source = EXCLUDED.source,
    updated_at = EXCLUDED.updated_at,
    updated_by = EXCLUDED.updated_by
"""

DELETE_SQL = "DELETE FROM app_config WHERE key = $1"


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

    def __init__(self, db: asyncpg.Pool | asyncpg.Connection | Any) -> None:
        self._db = db

    @contextlib.asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        if hasattr(self._db, "acquire"):
            async with self._db.acquire() as conn:
                yield conn
        else:
            yield self._db

    async def ensure_schema(self) -> None:
        """Create the table when it is missing."""
        async with self._connection() as conn:
            await conn.execute(CREATE_TABLE_SQL)

    async def fetch_all(self) -> dict[str, ConfigRow]:
        """Return every stored setting, keyed by name."""
        async with self._connection() as conn:
            records = await conn.fetch(SELECT_ALL_SQL)
        return {
            record["key"]: ConfigRow(
                key=record["key"],
                value=record["value"],
                secret=record["secret"],
                source=record["source"],
                updated_at=record["updated_at"],
                updated_by=record["updated_by"],
            )
            for record in records
        }

    async def insert_missing(self, rows: Sequence[ConfigRow]) -> list[str]:
        """Insert the rows whose keys are not stored yet; return the keys added."""
        if not rows:
            return []
        async with self._connection() as conn:
            records = await conn.fetch(
                INSERT_MISSING_SQL,
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

    async def delete(self, key: str) -> bool:
        """Remove one stored setting; return whether a row existed."""
        async with self._connection() as conn:
            status = await conn.execute(DELETE_SQL, key)
        return status.rsplit(" ", 1)[-1] != "0"

    async def api_keys_exist(self) -> bool:
        """Return whether any user API key has been issued."""
        async with self._connection() as conn:
            if await conn.fetchval("SELECT to_regclass('api_keys')") is None:
                return False
            return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM api_keys)"))


async def connect(settings: Settings) -> asyncpg.Connection:
    """Open the short-lived connection the boot-time configuration load uses."""
    return await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        timeout=CONNECT_TIMEOUT_SECONDS,
        command_timeout=COMMAND_TIMEOUT_SECONDS,
    )
