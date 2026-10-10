"""In-memory stand-in for ``serving.config.app_config_store.AppConfigStore``."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from serving.config.app_config_store import ConfigRow

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class FakeAppConfigStore:
    """Holds ``app_config`` rows in a dict, with the store's conflict semantics."""

    def __init__(self, rows: Iterable[ConfigRow] = (), *, api_keys_exist: bool = False) -> None:
        self.rows: dict[str, ConfigRow] = {row.key: row for row in rows}
        self.api_keys = api_keys_exist
        self.schema_calls = 0
        self.writes: list[list[ConfigRow]] = []
        self.deleted: list[str] = []

    @staticmethod
    def _stamped(row: ConfigRow) -> ConfigRow:
        return replace(row, updated_at=datetime.now(timezone.utc))

    async def ensure_schema(self) -> None:
        self.schema_calls += 1

    async def fetch_all(self) -> dict[str, ConfigRow]:
        return dict(self.rows)

    async def insert_missing(self, rows: Sequence[ConfigRow]) -> list[str]:
        added = []
        for row in rows:
            if row.key not in self.rows:
                self.rows[row.key] = self._stamped(row)
                added.append(row.key)
        return added

    async def write(self, rows: Sequence[ConfigRow]) -> None:
        self.writes.append(list(rows))
        for row in rows:
            self.rows[row.key] = self._stamped(row)

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
