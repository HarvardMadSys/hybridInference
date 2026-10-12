"""In-memory stand-in for ``serving.config.app_config_store.AppConfigStore``."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from serving.config.app_config_store import RESET_SOURCE, ConfigRow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Sequence


class FakeAppConfigStore:
    """Holds ``app_config`` rows in a dict, with the store's conflict semantics.

    Reset rows are kept in :attr:`rows` like the table keeps them, and left out
    of :meth:`fetch_all` like the store leaves them out.
    """

    def __init__(self, rows: Iterable[ConfigRow] = (), *, api_keys_exist: bool = False) -> None:
        self.rows: dict[str, ConfigRow] = {row.key: row for row in rows}
        self.api_keys = api_keys_exist
        self.schema_calls = 0
        self.writes: list[list[ConfigRow]] = []
        self.deleted: list[str] = []
        self.resets: list[str] = []
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[FakeAppConfigStore]:
        async with self._lock:
            before = self.rows.copy()
            try:
                yield self
            except BaseException:
                self.rows = before
                raise

    @staticmethod
    def _stamped(row: ConfigRow) -> ConfigRow:
        return replace(row, updated_at=datetime.now(timezone.utc))

    async def ensure_schema(self) -> None:
        self.schema_calls += 1

    async def fetch_all(self) -> dict[str, ConfigRow]:
        return {key: row for key, row in self.rows.items() if row.source != RESET_SOURCE}

    async def insert_missing(
        self, rows: Sequence[ConfigRow], *, replace_reset: bool = False
    ) -> list[str]:
        added = []
        for row in rows:
            existing = self.rows.get(row.key)
            if existing is None or (replace_reset and existing.source == RESET_SOURCE):
                self.rows[row.key] = self._stamped(row)
                added.append(row.key)
        return added

    async def write(self, rows: Sequence[ConfigRow]) -> None:
        self.writes.append(list(rows))
        for row in rows:
            self.rows[row.key] = self._stamped(row)

    async def reset(self, key: str, *, secret: bool, updated_by: str) -> None:
        self.resets.append(key)
        self.rows[key] = self._stamped(
            ConfigRow(key, "", secret=secret, source=RESET_SOURCE, updated_by=updated_by)
        )

    async def delete(self, key: str) -> bool:
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
        updated_at=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
        updated_by="admin@example.com",
    )
