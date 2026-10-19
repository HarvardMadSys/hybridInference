"""In-memory stand-in for ``serving.config.app_config_store.AppConfigStore``."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from serving.config import app_config_registry as registry
from serving.config.app_config_store import RESET_SOURCE, ConfigOwnershipError, ConfigRow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Sequence


class FakeAppConfigStore:
    """Holds ``app_config`` rows in a dict, with the store's conflict semantics.

    Reset rows are kept in :attr:`rows` like the table keeps them, and left out
    of :meth:`fetch_all` like the store leaves them out. Like the table, no
    write clears a row's secret flag.
    """

    def __init__(self, rows: Iterable[ConfigRow] = (), *, api_keys_exist: bool = False) -> None:
        self.rows: dict[str, ConfigRow] = {row.key: row for row in rows}
        self.api_keys = api_keys_exist
        self.schema_calls = 0
        self.writes: list[list[ConfigRow]] = []
        self.deleted: list[str] = []
        self.resets: list[str] = []
        self.marked: list[str] = []
        self._lock = asyncio.Lock()
        self._transaction_task = None
        self.migrations = {}

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[FakeAppConfigStore]:
        task = asyncio.current_task()
        nested = self._transaction_task is task
        if not nested:
            await self._lock.acquire()
            self._transaction_task = task
        before = self.rows.copy()
        migrations_before = self.migrations.copy()
        try:
            yield self
        except BaseException:
            self.rows = before
            self.migrations = migrations_before
            raise
        finally:
            if not nested:
                self._transaction_task = None
                self._lock.release()

    async def apply_migrations(self, migrations, *, core_keys=(), updated_by=None):
        async with self.transaction():
            self._claim(core_keys, "core", ())
            for migration in migrations:
                previous = self.migrations.get(migration.id)
                if previous is not None:
                    if previous[:2] != (migration.owner, migration.checksum):
                        raise ConfigOwnershipError(
                            f"Configuration ownership migration changed: {migration.id}"
                        )
                    continue
                self._claim(migration.keys, migration.owner, migration.secret_keys)
                self.migrations[migration.id] = (migration.owner, migration.checksum, updated_by)

    def _claim(self, keys, owner, secret_keys):
        for key in keys:
            existing = self.rows.get(key)
            if existing is None:
                continue
            if existing.owner is not None and existing.owner != owner:
                raise ConfigOwnershipError(f"Configuration ownership conflict: {key}")
            self.rows[key] = replace(
                existing, owner=owner, secret=existing.secret or key in secret_keys
            )

    async def adopt_custom(self, key, *, secret=False, updated_by=None):
        async with self.transaction():
            existing = self.rows.get(key)
            if existing is None or existing.owner is not None:
                raise ConfigOwnershipError(f"Configuration is not unclassified: {key}")
            await self.apply_migrations(
                [
                    registry.ConfigOwnershipMigration(
                        id=f"custom-adoption:{key}:{uuid4()}",
                        owner="custom",
                        keys=(key,),
                        secret_keys=(key,) if secret else (),
                    )
                ],
                updated_by=updated_by,
            )

    def _check_owner(self, key, owner):
        if owner is None or (key in self.rows and self.rows[key].owner != owner):
            raise ConfigOwnershipError(f"Configuration ownership conflict: {key}")

    @staticmethod
    def _stamped(row: ConfigRow) -> ConfigRow:
        return replace(row, updated_at=datetime.now(timezone.utc))

    async def ensure_schema(self) -> None:
        self.schema_calls += 1

    async def fetch_all(self, *, include_reset: bool = False) -> dict[str, ConfigRow]:
        return {
            key: row
            for key, row in self.rows.items()
            if include_reset or row.source != RESET_SOURCE
        }

    def _kept_secret(self, row: ConfigRow) -> ConfigRow:
        existing = self.rows.get(row.key)
        if existing is not None and existing.secret and not row.secret:
            return replace(row, secret=True)
        return row

    async def insert_missing(
        self, rows: Sequence[ConfigRow], *, replace_reset: bool = False
    ) -> list[str]:
        added = []
        for row in rows:
            self._check_owner(row.key, row.owner)
            existing = self.rows.get(row.key)
            if existing is None or (replace_reset and existing.source == RESET_SOURCE):
                self.rows[row.key] = self._stamped(self._kept_secret(row))
                added.append(row.key)
        return added

    async def write(self, rows: Sequence[ConfigRow]) -> None:
        self.writes.append(list(rows))
        for row in rows:
            self._check_owner(row.key, row.owner)
            self.rows[row.key] = self._stamped(self._kept_secret(row))

    async def mark_secret(self, keys: Sequence[str], *, owner: str) -> list[str]:
        marked = []
        for key in keys:
            self._check_owner(key, owner)
            existing = self.rows.get(key)
            if existing is not None and not existing.secret:
                # The value and its updated_* columns are left alone.
                self.rows[key] = replace(existing, secret=True)
                marked.append(key)
        self.marked.extend(marked)
        return marked

    async def reset(self, key: str, *, secret: bool, updated_by: str, owner: str) -> None:
        self._check_owner(key, owner)
        self.resets.append(key)
        self.rows[key] = self._stamped(
            self._kept_secret(
                ConfigRow(
                    key, "", secret=secret, source=RESET_SOURCE, updated_by=updated_by, owner=owner
                )
            )
        )

    async def delete(self, key: str, *, owner: str) -> bool:
        self._check_owner(key, owner)
        self.deleted.append(key)
        return self.rows.pop(key, None) is not None

    async def api_keys_exist(self) -> bool:
        return self.api_keys


def row(
    key: str, value: str, *, secret: bool = False, source: str = "admin", owner: str | None = "auto"
) -> ConfigRow:
    """Build an already-owned seed; pass owner=None for an unclassified legacy row."""
    if owner == "auto":
        entry = registry.static_entry(key)
        owner = entry.owner if entry is not None else "custom"
    return ConfigRow(
        key,
        value,
        secret=secret,
        source=source,
        updated_at=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
        updated_by="admin@example.com",
        owner=owner,
    )
