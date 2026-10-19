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
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import asyncpg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from datetime import datetime

    from serving.config.app_config_registry import ConfigOwnershipMigration
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
    updated_by  TEXT,
    owner       TEXT
)
"""

#: The ``source`` of a row that marks a reset instead of storing a value.
RESET_SOURCE = "reset"

SELECT_ALL_WITH_RESET_SQL = (
    "SELECT key, value, secret, source, updated_at, updated_by, owner FROM app_config"
)

SELECT_ALL_SQL = f"{SELECT_ALL_WITH_RESET_SQL} WHERE source <> '{RESET_SOURCE}'"

# One-way: a secret is never made public again. A reset row is marked too, so
# the setting stays secret while it follows the environment. The value and the
# updated_* columns describe the value, which does not change.
MARK_SECRET_SQL = """
UPDATE app_config SET secret = TRUE
WHERE key = ANY($1::text[]) AND NOT secret AND owner = $2
RETURNING key
"""

# Rows that do not exist yet, and only those: several workers booting at once
# all try the same import, and an administrator's value must never be
# overwritten by an environment one. A reset row conflicts too, which is what
# keeps a reset setting following the environment. RETURNING names the rows
# this call added.
INSERT_MISSING_SQL = """
INSERT INTO app_config (key, value, secret, source, updated_by, owner)
SELECT * FROM unnest($1::text[], $2::text[], $3::boolean[], $4::text[], $5::text[], $6::text[])
ON CONFLICT (key) DO NOTHING
RETURNING key
"""

# The same, except that a reset row is replaced: for a generated secret, which
# must exist once neither the table nor the environment holds one. The row lock
# makes a concurrent worker re-check the condition against the winner's row.
INSERT_MISSING_OR_RESET_SQL = f"""
INSERT INTO app_config (key, value, secret, source, updated_by, owner)
SELECT * FROM unnest($1::text[], $2::text[], $3::boolean[], $4::text[], $5::text[], $6::text[])
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    secret = app_config.secret OR EXCLUDED.secret,
    source = EXCLUDED.source,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by
WHERE app_config.source = '{RESET_SOURCE}' AND app_config.owner = EXCLUDED.owner
RETURNING key
"""

# Neither a write nor a reset clears the secret flag: it is one-way.
UPSERT_SQL = """
INSERT INTO app_config (key, value, secret, source, updated_at, updated_by, owner)
VALUES ($1, $2, $3, $4, NOW(), $5, $6)
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    secret = app_config.secret OR EXCLUDED.secret,
    source = EXCLUDED.source,
    updated_at = EXCLUDED.updated_at,
    updated_by = EXCLUDED.updated_by
WHERE app_config.owner = EXCLUDED.owner
RETURNING key
"""

RESET_SQL = f"""
INSERT INTO app_config (key, value, secret, source, updated_at, updated_by, owner)
VALUES ($1, '', $2, '{RESET_SOURCE}', NOW(), $3, $4)
ON CONFLICT (key) DO UPDATE SET
    value = '',
    secret = app_config.secret OR EXCLUDED.secret,
    source = '{RESET_SOURCE}',
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by
WHERE app_config.owner = EXCLUDED.owner
RETURNING key
"""

DELETE_SQL = "DELETE FROM app_config WHERE key = $1 AND owner = $2"

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
    #: NULL denotes an unclassified legacy row; no ordinary write can claim it.
    owner: str | None = None


class ConfigOwnershipError(RuntimeError):
    """A claim or write would cross a configuration owner's boundary."""


CREATE_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS app_config_ownership_migrations (
    id          TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by  TEXT
)
"""


def _require_owner(owner: str | None) -> str:
    if owner not in ("core", "custom"):
        from serving.config.app_config_registry import _require_extension_owner

        if owner is None:
            raise ConfigOwnershipError("A configuration write requires an explicit owner")
        try:
            _require_extension_owner(owner)
        except ValueError as exc:
            raise ConfigOwnershipError(str(exc)) from exc
    return owner


async def _check_owners(conn: Any, expected: dict[str, str]) -> None:
    records = await conn.fetch(
        "SELECT key, owner FROM app_config WHERE key = ANY($1::text[]) FOR UPDATE",
        list(expected),
    )
    for record in records:
        if record["owner"] != expected[record["key"]]:
            raise ConfigOwnershipError(f"Configuration ownership conflict: {record['key']}")


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
        """Upgrade the schema without claiming or changing historical values."""
        async with self.transaction() as transaction, transaction._connection() as conn:
            await conn.execute(CREATE_TABLE_SQL)
            await conn.execute("ALTER TABLE app_config ADD COLUMN IF NOT EXISTS owner TEXT")
            await conn.execute(CREATE_MIGRATIONS_SQL)

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[AppConfigStore]:
        """Serialize a read/validate/write operation on one database connection.

        Acquire the lock before reading. READ COMMITTED then sees the previous
        writer's commit, even when this transaction waited for it. Release both
        the transaction and the lock before reloading the process's cache.
        """
        async with self._connection() as conn, conn.transaction(isolation="read_committed"):
            await conn.execute(CONFIG_WRITE_LOCK_SQL)
            yield AppConfigStore(conn)

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
        return {
            record["key"]: ConfigRow(
                key=record["key"],
                value=record["value"],
                secret=record["secret"],
                source=record["source"],
                updated_at=record["updated_at"],
                updated_by=record["updated_by"],
                owner=record["owner"],
            )
            for record in records
        }

    async def apply_migrations(
        self,
        migrations: Sequence[ConfigOwnershipMigration],
        *,
        core_keys: Sequence[str] = (),
        updated_by: str | None = None,
    ) -> None:
        """Claim only explicitly declared historical rows, atomically and once.

        Existing values, sources and value audit stamps are preserved. Schema,
        claims and concurrent worker writes share the same transaction lock.
        """
        async with self.transaction() as transaction, transaction._connection() as conn:
            if core_keys:
                await self._claim(conn, core_keys, "core", ())
            for migration in migrations:
                _require_owner(migration.owner)
                previous = await conn.fetchrow(
                    "SELECT owner, checksum FROM app_config_ownership_migrations WHERE id = $1",
                    migration.id,
                )
                if previous is not None:
                    if (
                        previous["owner"] != migration.owner
                        or previous["checksum"] != migration.checksum
                    ):
                        raise ConfigOwnershipError(
                            f"Configuration ownership migration changed: {migration.id}"
                        )
                    continue
                await self._claim(conn, migration.keys, migration.owner, migration.secret_keys)
                await conn.execute(
                    "INSERT INTO app_config_ownership_migrations (id, owner, checksum, applied_by) "
                    "VALUES ($1, $2, $3, $4)",
                    migration.id,
                    migration.owner,
                    migration.checksum,
                    updated_by,
                )

    @staticmethod
    async def _claim(
        conn: Any, keys: Sequence[str], owner: str, secret_keys: Sequence[str]
    ) -> None:
        conflicts = await conn.fetch(
            "SELECT key FROM app_config WHERE key = ANY($1::text[]) "
            "AND owner IS NOT NULL AND owner <> $2 FOR UPDATE",
            list(keys),
            owner,
        )
        if conflicts:
            names = ", ".join(sorted(record["key"] for record in conflicts))
            raise ConfigOwnershipError(f"Configuration ownership conflict: {names}")
        await conn.execute(
            "UPDATE app_config SET owner = $2, secret = secret OR key = ANY($3::text[]) "
            "WHERE key = ANY($1::text[]) AND (owner IS NULL OR owner = $2)",
            list(keys),
            owner,
            list(secret_keys),
        )

    async def adopt_custom(
        self, key: str, *, secret: bool = False, updated_by: str | None = None
    ) -> None:
        """Explicitly adopt one unclassified row, preserving it and recording the claim."""
        from serving.config.app_config_registry import ConfigOwnershipMigration

        async with self.transaction() as transaction:
            async with transaction._connection() as conn:
                record = await conn.fetchrow(
                    "SELECT owner FROM app_config WHERE key = $1 FOR UPDATE", key
                )
                if record is None or record["owner"] is not None:
                    raise ConfigOwnershipError(f"Configuration is not unclassified: {key}")
            await transaction.apply_migrations(
                [
                    ConfigOwnershipMigration(
                        id=f"custom-adoption:{key}:{uuid4()}",
                        owner="custom",
                        keys=(key,),
                        secret_keys=(key,) if secret else (),
                    )
                ],
                updated_by=updated_by,
            )

    async def insert_missing(
        self, rows: Sequence[ConfigRow], *, replace_reset: bool = False
    ) -> list[str]:
        """Insert absent rows, optionally replacing same-owner reset markers."""
        if not rows:
            return []
        expected = {row.key: _require_owner(row.owner) for row in rows}
        async with self.transaction() as transaction, transaction._connection() as conn:
            await _check_owners(conn, expected)
            records = await conn.fetch(
                INSERT_MISSING_OR_RESET_SQL if replace_reset else INSERT_MISSING_SQL,
                [row.key for row in rows],
                [row.value for row in rows],
                [row.secret for row in rows],
                [row.source for row in rows],
                [row.updated_by for row in rows],
                [row.owner for row in rows],
            )
        return [record["key"] for record in records]

    async def write(self, rows: Sequence[ConfigRow]) -> None:
        """Insert or replace rows owned by the writer, atomically."""
        if not rows:
            return
        expected = {row.key: _require_owner(row.owner) for row in rows}
        async with self.transaction() as transaction, transaction._connection() as conn:
            await _check_owners(conn, expected)
            for row in rows:
                key = await conn.fetchval(
                    UPSERT_SQL,
                    row.key,
                    row.value,
                    row.secret,
                    row.source,
                    row.updated_by,
                    row.owner,
                )
                if key is None:
                    raise ConfigOwnershipError(f"Configuration ownership conflict: {row.key}")

    async def mark_secret(self, keys: Sequence[str], *, owner: str) -> list[str]:
        """Make same-owner rows secret without changing their value audit stamps."""
        if not keys:
            return []
        _require_owner(owner)
        async with self.transaction() as transaction, transaction._connection() as conn:
            await _check_owners(conn, dict.fromkeys(keys, owner))
            records = await conn.fetch(MARK_SECRET_SQL, list(keys), owner)
        return [record["key"] for record in records]

    async def reset(self, key: str, *, secret: bool, updated_by: str, owner: str) -> None:
        """Replace a same-owner value with a reset marker, retaining ownership."""
        _require_owner(owner)
        async with self.transaction() as transaction, transaction._connection() as conn:
            await _check_owners(conn, {key: owner})
            changed = await conn.fetchval(RESET_SQL, key, secret, updated_by, owner)
            if changed is None:
                raise ConfigOwnershipError(f"Configuration ownership conflict: {key}")

    async def delete(self, key: str, *, owner: str) -> bool:
        """Remove a same-owner row; an unclassified or foreign row is never deleted."""
        _require_owner(owner)
        async with self.transaction() as transaction, transaction._connection() as conn:
            await _check_owners(conn, {key: owner})
            status = await conn.execute(DELETE_SQL, key, owner)
        return status.rsplit(" ", 1)[-1] != "0"

    async def api_keys_exist(self) -> bool:
        """Return whether any user API key has been issued."""
        async with self._connection() as conn:
            if await conn.fetchval("SELECT to_regclass('api_keys')") is None:
                return False
            return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM api_keys)"))


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
