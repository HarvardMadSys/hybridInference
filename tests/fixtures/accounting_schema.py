"""The native accounting columns, for tests that build a private table from the baseline.

Some tests build ``api_logs`` (or another table) in a schema of their own with
the frozen baseline builder, which predates ``0022_ledger_currency``. The
gateway's writers name the native columns that revision adds, so such a table
needs them too. They are read from the revision itself, never restated here.
"""

from __future__ import annotations

import importlib.util
from functools import cache
from typing import TYPE_CHECKING, Any

from migrations import MIGRATIONS_DIR

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import ModuleType

REVISION = "0022_ledger_currency"


@cache
def revision() -> ModuleType:
    """The ``0022_ledger_currency`` revision module, loaded from its file."""
    path = MIGRATIONS_DIR / "versions" / f"{REVISION}.py"
    spec = importlib.util.spec_from_file_location(f"_revision_{REVISION}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def add_native_columns(conn: Any, tables: Iterable[str]) -> None:
    """Add the revision's native amount and currency columns to *tables* in the current schema."""
    wanted = set(tables)
    for table, _trigger, pairs in revision().TRIGGERS:
        if table not in wanted:
            continue
        for column in (*pairs.values(), "accounting_currency"):
            kind = "TEXT" if column == "accounting_currency" else "NUMERIC(24, 12)"
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
