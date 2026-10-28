"""In-memory stand-in for ``serving.config.app_config_store.AppConfigStore``."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from serving.config import revisions
from serving.config.app_config_store import RESET_SOURCE, ConfigRow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Sequence


class FakeAppConfigStore:
    """Holds ``app_config`` rows in a dict, with the store's conflict semantics.

    Reset rows are kept in :attr:`rows` like the table keeps them, and left out
    of :meth:`fetch_all` like the store leaves them out. Like the table, no
    write clears a row's secret flag. Every write call advances
    :attr:`revision`, as the table's statement trigger does, whether or not it
    changed a row, and a rolled-back transaction takes its advances back.
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
        self.epoch = "fakeepoch0000000"
        self.revision = 0
        self.contracts = revisions.Contracts()
        self.locked_version: revisions.SourceVersion | None = None

    @property
    def version(self) -> revisions.SourceVersion:
        return revisions.SourceVersion(self.epoch, self.revision)

    def _bump(self) -> None:
        self.revision += 1

    @asynccontextmanager
    async def transaction(
        self, *, expected: revisions.SourceVersion | None = None
    ) -> AsyncIterator[FakeAppConfigStore]:
        async with self._lock:
            revisions.check_expected(expected, self.version)
            before = (self.rows.copy(), self.revision, self.contracts)
            self.locked_version = self.version
            try:
                yield self
            except BaseException:
                self.rows, self.revision, self.contracts = before
                raise
            finally:
                self.locked_version = None

    async def current_version(self) -> revisions.SourceVersion:
        return self.version

    async def fetch_snapshot(self) -> revisions.SourceSnapshot:
        # Through fetch_all, so a test that slows or breaks reads there slows
        # or breaks this one too. The version is taken first, as one snapshot.
        version, contracts = self.version, self.contracts
        rows = await self.fetch_all(include_reset=True)
        return revisions.SourceSnapshot(
            source=revisions.APP_CONFIG_SOURCE,
            version=version,
            contracts=contracts,
            rows=dict(rows),
        )

    def set_contract(self, name: str, *, active: bool, schema_version: int | None = None) -> None:
        """Activate or deactivate a group's contract, as the deployment command does."""
        from serving.config.app_config_registry import MANAGED_GROUPS

        manifest = MANAGED_GROUPS[name]
        groups = dict(self.contracts.groups)
        groups[name] = revisions.GroupContract(
            name,
            schema_version or manifest.schema_version,
            manifest.keys,
            active=active,
        )
        self.contracts = revisions.Contracts(groups)
        self._bump()

    @staticmethod
    def _stamped(row: ConfigRow) -> ConfigRow:
        return replace(row, updated_at=datetime.now(UTC))

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
        if not rows:
            return []
        self._bump()
        added = []
        for row in rows:
            existing = self.rows.get(row.key)
            if existing is None or (replace_reset and existing.source == RESET_SOURCE):
                self.rows[row.key] = self._stamped(self._kept_secret(row))
                added.append(row.key)
        return added

    async def write(self, rows: Sequence[ConfigRow]) -> None:
        if not rows:
            return
        self._bump()
        self.writes.append(list(rows))
        for row in rows:
            self.rows[row.key] = self._stamped(self._kept_secret(row))

    async def mark_secret(self, keys: Sequence[str]) -> list[str]:
        if not keys:
            return []
        self._bump()
        marked = []
        for key in keys:
            existing = self.rows.get(key)
            if existing is not None and not existing.secret:
                # The value and its updated_* columns are left alone.
                self.rows[key] = replace(existing, secret=True)
                marked.append(key)
        self.marked.extend(marked)
        return marked

    async def reset(self, key: str, *, secret: bool, updated_by: str) -> None:
        self._bump()
        self.resets.append(key)
        self.rows[key] = self._stamped(
            self._kept_secret(
                ConfigRow(key, "", secret=secret, source=RESET_SOURCE, updated_by=updated_by)
            )
        )

    async def delete(self, key: str) -> bool:
        self._bump()
        self.deleted.append(key)
        return self.rows.pop(key, None) is not None

    async def api_keys_exist(self) -> bool:
        return self.api_keys


def row(key: str, value: str, *, secret: bool = False, source: str = "admin") -> ConfigRow:
    """Build a stored row the way the store returns it."""
    return ConfigRow(
        key,
        value,
        secret=secret,
        source=source,
        updated_at=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
        updated_by="admin@example.com",
    )
