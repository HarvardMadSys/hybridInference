"""Import the Cloudflare D1 database the OpenInfra dashboard kept before it moved into the gateway.

    npx wrangler d1 export <database> --remote --output openinfra.sql
    python -m serving.openinfra.import_d1 openinfra.sql [--dry-run]

The input is that export (SQL text, read a batch of statements at a time into a
temporary SQLite file, so its size does not matter) or a SQLite database made
from it. Every row is copied into the ``openinfra_*`` tables: hosts, GPUs and
engines, their samples, request events and rollups. Columns an older database
lacks are left empty, and a sample keeps the first of its 6-second bucket, as
the gateway does.

What the gateway already holds wins, so the import can run after the reporters
were pointed at the gateway, and again: a host, GPU or engine row, a sample
bucket or a rollup bucket it has is kept, and request events are imported only
from before each engine's first one here. Engines the old dashboard had retired
appear again unless ``OPENINFRA_RETIRED_ENGINES`` lists them.

The connection is the gateway's own database settings (``DB_HOST`` and the
rest, as every ``python -m serving`` job reads them). Writes go in chunks of
:data:`CHUNK_ROWS`; nothing here creates or alters a table.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from serving.openinfra.store import SAMPLE_MS
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = get_logger(__name__)

#: Rows written per statement.
CHUNK_ROWS = 5_000
#: SQL text loaded into SQLite per batch, in characters.
_LOAD_BATCH_CHARS = 4 * 1024 * 1024
_SQLITE_HEADER = b"SQLite format 3\x00"


@dataclass(frozen=True)
class Table:
    """One D1 table and the gateway table its rows go to."""

    source: str
    target: str
    #: Primary key the gateway keeps on a conflict; None for request events.
    key: tuple[str, ...] | None
    renames: dict[str, str] = field(default_factory=dict)
    #: Whether the target has a ``bucket`` column computed from ``ts``.
    bucketed: bool = False


TABLES: tuple[Table, ...] = (
    Table("nodes", "openinfra_nodes", ("id",)),
    Table("gpus", "openinfra_gpus", ("id",)),
    Table("sglang_servers", "openinfra_engines", ("id",)),
    Table("cpu_samples", "openinfra_cpu_samples", ("node_id", "bucket"), bucketed=True),
    Table("gpu_samples", "openinfra_gpu_samples", ("gpu_id", "bucket"), bucketed=True),
    Table(
        "sglang_samples",
        "openinfra_engine_samples",
        ("engine_id", "bucket"),
        {"server_id": "engine_id"},
        bucketed=True,
    ),
    Table("gpu_rollups", "openinfra_gpu_rollups", ("grain", "bucket_ts", "gpu_id")),
    Table(
        "sglang_rollups",
        "openinfra_engine_rollups",
        ("grain", "bucket_ts", "engine_id"),
        {"server_id": "engine_id"},
    ),
    Table("cpu_rollups", "openinfra_cpu_rollups", ("grain", "bucket_ts", "node_id")),
    Table("request_events", "openinfra_request_events", None, {"server_id": "engine_id"}),
)

#: Values a D1 row may lack that a gateway column requires.
_FILL: dict[str, Any] = {"status": "unknown", "samples": 0}


@dataclass(frozen=True)
class Column:
    """A gateway column: its name, type and whether it may be empty."""

    name: str
    pg_type: str
    nullable: bool


def _integer(value: Any) -> int | None:
    number = _double(value)
    return math.floor(number + 0.5) if number is not None else None


def _double(value: Any) -> float | None:
    if value is None or isinstance(value, bytes):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _jsonb(value: Any) -> str | None:
    """JSON text for a JSONB column; what does not parse is dropped."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            json.loads(value)
        except ValueError:
            return None
        return value
    return json.dumps(value)


_CONVERT: dict[str, Callable[[Any], Any]] = {
    "bigint": _integer,
    "integer": _integer,
    "double precision": _double,
    "text": _text,
    "jsonb": _jsonb,
}
_ARRAY_TYPE = {
    "bigint": "bigint",
    "integer": "integer",
    "double precision": "double precision",
    "text": "text",
    "jsonb": "text",
}


def load_dump(path: Path, database: Path) -> None:
    """Load a D1 SQL export into the SQLite file *database*, a batch of statements at a time."""
    conn = sqlite3.connect(database, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        buffered: list[str] = []
        size = 0
        with path.open(encoding="utf-8") as source:
            for line in source:
                buffered.append(line)
                size += len(line)
                if size >= _LOAD_BATCH_CHARS:
                    script = "".join(buffered)
                    if sqlite3.complete_statement(script):
                        conn.executescript(script)
                        buffered, size = [], 0
        if buffered:
            conn.executescript("".join(buffered))
    finally:
        conn.close()


@contextlib.contextmanager
def open_source(path: Path) -> Iterator[sqlite3.Connection]:
    """The D1 data as a SQLite connection, loading an SQL export into a temporary file first."""
    with path.open("rb") as handle:
        is_database = handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    if is_database:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            yield conn
        finally:
            conn.close()
        return
    with tempfile.TemporaryDirectory(prefix="openinfra-import-") as directory:
        database = Path(directory) / "d1.sqlite"
        load_dump(path, database)
        conn = sqlite3.connect(database)
        try:
            yield conn
        finally:
            conn.close()


def source_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """The columns of a D1 table; empty when the export has no such table."""
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


async def target_columns(conn: Any, table: str) -> dict[str, Column]:
    """The gateway table's columns, in order, except a generated identity."""
    rows = await conn.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1 AND is_identity = 'NO' "
        "ORDER BY ordinal_position",
        table,
    )
    return {
        row["column_name"]: Column(
            row["column_name"], row["data_type"], row["is_nullable"] == "YES"
        )
        for row in rows
    }


@dataclass
class Plan:
    """How one D1 table's rows become gateway rows."""

    table: Table
    #: (source column, gateway column) pairs, in gateway column order.
    pairs: list[tuple[str, Column]]
    bucket: Column | None

    @property
    def columns(self) -> list[Column]:
        """The gateway columns written, the computed bucket last."""
        return [column for _, column in self.pairs] + ([self.bucket] if self.bucket else [])

    def convert(self, row: tuple[Any, ...]) -> tuple[Any, ...] | None:
        """A D1 row as gateway values; None when a required value is missing."""
        values = []
        for raw, (_, column) in zip(row, self.pairs, strict=True):
            value = _CONVERT[column.pg_type](raw)
            if value is None:
                value = _FILL.get(column.name)
            if value is None and column.name == "hostname":
                value = _text(row[0])
            if value is None and not column.nullable:
                return None
            values.append(value)
        if self.bucket is not None:
            ts = values[[c.name for _, c in self.pairs].index("ts")]
            values.append(ts // SAMPLE_MS)
        return tuple(values)


def plan(table: Table, source: list[str], target: dict[str, Column]) -> Plan:
    """Match a D1 table's columns to the gateway table's, by name after renames."""
    renamed = {table.renames.get(name, name): name for name in source}
    # The id column leads, so a missing hostname can fall back to it.
    order = sorted(target, key=lambda name: name != "id")
    pairs = [
        (renamed[name], target[name])
        for name in order
        if name in renamed and not (table.bucketed and name == "bucket")
    ]
    return Plan(table, pairs, target.get("bucket") if table.bucketed else None)


def insert_sql(table_plan: Plan, *, before: bool = False) -> str:
    """One chunk's insert: column arrays unnested, earliest first, existing rows kept."""
    columns = table_plan.columns
    names = ", ".join(column.name for column in columns)
    arrays = ", ".join(
        f"${i}::{_ARRAY_TYPE[column.pg_type]}[]" for i, column in enumerate(columns, start=1)
    )
    values = ", ".join(
        f"u.{column.name}::jsonb" if column.pg_type == "jsonb" else f"u.{column.name}"
        for column in columns
    )
    order = " ORDER BY u.ts" if any(column.name == "ts" for column in columns) else ""
    conflict = (
        f" ON CONFLICT ({', '.join(table_plan.table.key)}) DO NOTHING"
        if table_plan.table.key
        else ""
    )
    return (
        f"INSERT INTO {table_plan.table.target} ({names}) "
        f"SELECT {values} FROM unnest({arrays}) AS u({names}){order}{conflict}"
    )


def _chunks(cursor: sqlite3.Cursor) -> Iterator[list[tuple[Any, ...]]]:
    while True:
        rows = cursor.fetchmany(CHUNK_ROWS)
        if not rows:
            return
        yield rows


async def import_table(
    source: sqlite3.Connection, conn: Any, table: Table, *, dry_run: bool = False
) -> tuple[int, int]:
    """Copy one table; (rows read, rows refused for a missing required value)."""
    columns = source_columns(source, table.source)
    if not columns:
        return 0, 0
    table_plan = plan(table, columns, await target_columns(conn, table.target))
    sql = insert_sql(table_plan)
    cutoff: dict[str, int] = {}
    if table.key is None:
        cutoff = {
            row["engine_id"]: row["first"]
            for row in await conn.fetch(
                "SELECT engine_id, MIN(ts) AS first FROM openinfra_request_events "
                "GROUP BY engine_id"
            )
        }
    selected = ", ".join(f'"{name}"' for name, _ in table_plan.pairs)
    cursor = source.execute(f'SELECT {selected} FROM "{table.source}" ORDER BY rowid')
    read = refused = 0
    for rows in _chunks(cursor):
        read += len(rows)
        converted = [values for values in map(table_plan.convert, rows) if values is not None]
        refused += len(rows) - len(converted)
        if cutoff:
            engine = [c.name for c in table_plan.columns].index("engine_id")
            ts = [c.name for c in table_plan.columns].index("ts")
            converted = [v for v in converted if v[ts] < cutoff.get(v[engine], math.inf)]
        if converted and not dry_run:
            await conn.execute(sql, *(list(column) for column in zip(*converted, strict=True)))
    return read, refused


async def run(path: Path, conn: Any, *, dry_run: bool = False) -> dict[str, tuple[int, int]]:
    """Import every table of the export at *path*; per D1 table, (rows read, rows refused)."""
    results: dict[str, tuple[int, int]] = {}
    with open_source(path) as source:
        for table in TABLES:
            if dry_run:
                results[table.source] = await import_table(source, conn, table, dry_run=True)
                continue
            async with conn.transaction():
                results[table.source] = await import_table(source, conn, table)
            logger.info(
                "openinfra import: %s -> %s: %d rows read, %d refused",
                table.source,
                table.target,
                *results[table.source],
            )
    return results


async def _main(args: argparse.Namespace) -> int:
    import asyncpg

    from serving.config.settings import get_settings

    settings = get_settings()
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        server_settings={"application_name": "openinfra_import_d1"},
    )
    try:
        results = await run(args.path, conn, dry_run=args.dry_run)
    finally:
        await conn.close()
    verb = "would import" if args.dry_run else "imported"
    for name, (read, refused) in results.items():
        print(f"{name}: {verb} {read - refused} of {read} rows", file=sys.stdout)
    return 0


def cli(argv: list[str] | None = None) -> int:
    """Import an OpenInfra D1 export into this gateway's database."""
    parser = argparse.ArgumentParser(description=cli.__doc__)
    parser.add_argument("path", type=Path, help="wrangler d1 export output, or a SQLite file")
    parser.add_argument(
        "--dry-run", action="store_true", help="read and count the rows without writing them"
    )
    args = parser.parse_args(argv)
    if not os.path.isfile(args.path):
        parser.error(f"{args.path} is not a file")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(cli())
