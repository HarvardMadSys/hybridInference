"""Weekly archives of OpenInfra's tables, and the retention that deletes old samples.

**Weeks.** A week runs from Monday 00:00 UTC to the next (:func:`week_start`).
Once one has ended and :data:`ARCHIVE_GRACE_MS` has passed, the hourly archive
job writes it to ``<OPENINFRA_ARCHIVE_DIR>/<YYYY-MM-DD>/``, named for its Monday:
one gzip-compressed CSV file per table, with a header row, and
``manifest.json`` (the week, and each file's rows and bytes). Each week holds
its samples, request events and rollups, and the host, GPU and engine rows as
they were when it was written, so it can be read on its own. The files go to
a hidden directory beside it first and are renamed into place once complete,
so a week's directory is either whole or absent; the week is then recorded in
``openinfra_archives``, with the host that wrote it. A week with nothing in it
is skipped.

Nothing reaches an archived week afterwards: a snapshot's samples are stored
within :data:`~serving.openinfra.store.MAX_CLOCK_SKEW_MS` of the gateway's
clock, a request event older than
:data:`~serving.openinfra.store.MAX_REQUEST_AGE_MS` is dropped, both well
inside the grace period, and the D1 import holds the archive lock and leaves
archived weeks alone. So each table is read in short transactions, a key at a
time, rather than in one snapshot that would hold back vacuum for the whole
week; only the host, GPU and engine rows, which every snapshot changes, are
read together.

**Retention.** Samples and request events older than ``OPENINFRA_RETENTION_DAYS``
(7 by default; 0 keeps everything) are deleted, in batches. While there is an
archive directory, only rows of weeks already archived are, so nothing leaves
the database before it is on disk; without one, only while
``OPENINFRA_DELETE_UNARCHIVED`` is on. Live rows, rollups and archive records
are kept.

The archive job and the retention run on the scheduler
(:mod:`serving.openinfra.maintenance`), under locks of their own.
``python -m serving.openinfra.archive`` lists the archived weeks, or with
``--run`` runs both once, from an environment with the gateway's ``DB_*``
settings.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import shutil
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from serving.openinfra.store import SAMPLE_MS
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = get_logger(__name__)

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
#: 1970-01-05, the first Monday of the Unix epoch.
_FIRST_MONDAY_MS = 4 * DAY_MS
#: How long after a week ends it is archived: past the oldest request event a
#: snapshot may still bring, and the last rollup refresh of its final bucket.
ARCHIVE_GRACE_MS = 60 * 60_000
#: Most weeks archived in one run, so a long backlog (an imported history)
#: is worked through over several runs rather than holding up the rollups.
WEEKS_PER_RUN = 4
#: Rows one delete statement removes.
DELETE_BATCH = 20_000
#: Bytes of CSV gathered before they are compressed, off the event loop.
_COMPRESS_CHUNK = 1 << 20
#: Sort memory for writing a week: one key's week of samples sorts in memory.
_EXPORT_WORK_MEM = "128MB"
MANIFEST = "manifest.json"
FORMAT = "openinfra-archive/1"

#: The advisory lock an archive run holds, and the D1 import with it, so no
#: week is archived while rows are being imported into it; session-scoped.
_LOCK_KEY = "hashtextextended('hybridinference:openinfra-archive', 0)"
TRY_LOCK_SQL = f"SELECT pg_try_advisory_lock({_LOCK_KEY})"
LOCK_SQL = f"SELECT pg_advisory_lock({_LOCK_KEY})"
UNLOCK_SQL = f"SELECT pg_advisory_unlock({_LOCK_KEY})"


class ArchiveError(RuntimeError):
    """A week could not be archived; the message says why."""


@dataclass(frozen=True)
class Table:
    """One table as an archive reads it, and as retention prunes it."""

    name: str
    #: The column a week's rows are selected by, or ``None`` for every row.
    time: str | None = None
    #: Milliseconds per unit of :attr:`time`.
    unit_ms: int = 1
    #: The leading primary-key column, when its rows are read one key at a time.
    key: str | None = None
    #: Whether retention deletes its old rows.
    pruned: bool = False

    def bound(self, ms: int) -> int:
        """*ms* in this table's time unit."""
        return ms // self.unit_ms


TABLES: tuple[Table, ...] = (
    Table("openinfra_cpu_samples", "bucket", SAMPLE_MS, "node_id", pruned=True),
    Table("openinfra_gpu_samples", "bucket", SAMPLE_MS, "gpu_id", pruned=True),
    Table("openinfra_engine_samples", "bucket", SAMPLE_MS, "engine_id", pruned=True),
    Table("openinfra_request_events", "ts", 1, "engine_id", pruned=True),
    Table("openinfra_cpu_rollups", "bucket_ts"),
    Table("openinfra_gpu_rollups", "bucket_ts"),
    Table("openinfra_engine_rollups", "bucket_ts"),
    Table("openinfra_nodes"),
    Table("openinfra_gpus"),
    Table("openinfra_engines"),
)
PRUNED: tuple[Table, ...] = tuple(table for table in TABLES if table.pruned)


def week_start(ts: int) -> int:
    """The start (Monday 00:00 UTC, in milliseconds) of the week holding *ts*."""
    return (ts - _FIRST_MONDAY_MS) // WEEK_MS * WEEK_MS + _FIRST_MONDAY_MS


def week_name(start: int) -> str:
    """A week's directory name: its Monday, ``YYYY-MM-DD``."""
    return datetime.fromtimestamp(start / 1000, tz=UTC).strftime("%Y-%m-%d")


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_archivable_week(now: int) -> int:
    """The latest week that ended at least :data:`ARCHIVE_GRACE_MS` before *now*."""
    return week_start(now - ARCHIVE_GRACE_MS) - WEEK_MS


def _keys_cte(table: Table) -> str:
    """Every distinct key of *table*, one index probe each (a loose index scan)."""
    key, name = table.key, table.name
    return (
        f"WITH RECURSIVE k(id) AS (SELECT MIN({key}) FROM {name} UNION ALL "
        f"SELECT (SELECT MIN({key}) FROM {name} WHERE {key} > k.id) FROM k WHERE k.id IS NOT NULL) "
    )


async def table_keys(conn: Any, table: Table) -> list[str]:
    """The distinct keys of a keyed table."""
    rows = await conn.fetch(f"{_keys_cte(table)}SELECT id FROM k WHERE id IS NOT NULL")
    return [row["id"] for row in rows]


async def first_times(conn: Any, table: Table) -> dict[str, int]:
    """Each key's oldest row in a keyed table, in milliseconds: one index probe per key."""
    rows = await conn.fetch(
        f"{_keys_cte(table)}SELECT id, (SELECT MIN(t.{table.time}) FROM {table.name} t "
        f"WHERE t.{table.key} = k.id) AS first FROM k WHERE k.id IS NOT NULL"
    )
    return {
        row["id"]: int(row["first"]) * table.unit_ms for row in rows if row["first"] is not None
    }


async def next_time(conn: Any, at_or_after: int, tables: Sequence[Table] = PRUNED) -> int | None:
    """The time (milliseconds) of the oldest row at or after *at_or_after* in *tables*.

    ``None`` when there is none. Each key is one probe of the index it leads.
    """
    found: int | None = None
    for table in tables:
        value = await conn.fetchval(
            f"{_keys_cte(table)}SELECT MIN((SELECT MIN(t.{table.time}) FROM {table.name} t "
            f"WHERE t.{table.key} = k.id AND t.{table.time} >= $1)) FROM k WHERE k.id IS NOT NULL",
            table.bound(at_or_after),
        )
        if value is not None:
            ms = int(value) * table.unit_ms
            found = ms if found is None else min(found, ms)
    return found


async def oldest_time(conn: Any, tables: Sequence[Table] = PRUNED) -> int | None:
    """The time (milliseconds) of the oldest row in *tables*, or ``None`` when they are empty."""
    return await next_time(conn, 0, tables)


async def archived_weeks(conn: Any) -> dict[int, dict[str, Any]]:
    """Every archived week by its start: where it was written, its rows and when."""
    rows = await conn.fetch(
        "SELECT week_start, location, tables, archived_at FROM openinfra_archives ORDER BY week_start"
    )
    return {
        row["week_start"]: {
            "location": row["location"],
            "tables": json.loads(row["tables"])
            if isinstance(row["tables"], str)
            else row["tables"],
            "archived_at": row["archived_at"],
        }
        for row in rows
    }


# --- writing a week ------------------------------------------------------------


class _CompressedFile:
    """CSV from COPY, gzip-compressed into a file in a worker thread."""

    def __init__(self, path: Path) -> None:
        self._raw = path.open("wb")
        # No name or time in the header: the same rows give the same bytes.
        self._gzip = gzip.GzipFile(filename="", mode="wb", fileobj=self._raw, mtime=0)
        self._pending: list[bytes] = []
        self._size = 0

    async def write(self, chunk: bytes) -> None:
        self._pending.append(chunk)
        self._size += len(chunk)
        if self._size >= _COMPRESS_CHUNK:
            await self._flush()

    async def _flush(self) -> None:
        if self._pending:
            data = b"".join(self._pending)
            self._pending, self._size = [], 0
            await asyncio.to_thread(self._gzip.write, data)

    async def close(self) -> None:
        """Compress what is left and make the file durable."""
        await self._flush()
        await asyncio.to_thread(self._finish)

    def _finish(self) -> None:
        self._gzip.close()
        self._raw.flush()
        os.fsync(self._raw.fileno())
        self._raw.close()

    def discard(self) -> None:
        """Close without caring what was written: the directory is about to go."""
        self._gzip.close()
        self._raw.close()


def _copied(status: str) -> int:
    """The row count of a ``COPY n`` status."""
    return int(status.split()[-1])


async def _copy_sorted(conn: Any, query: str, *args: Any, output: Any) -> int:
    """COPY *query*'s rows as CSV, in a transaction of its own with room to sort; how many."""
    async with conn.transaction():
        await conn.execute(f"SET LOCAL work_mem = '{_EXPORT_WORK_MEM}'")
        return _copied(await conn.copy_from_query(query, *args, output=output, format="csv"))


async def _export(conn: Any, table: Table, path: Path, start: int, end: int) -> int:
    """Write *table*'s rows of the week [start, end) to *path*; the number of rows.

    A table with no time column is written whole, in the caller's transaction.
    """
    out = _CompressedFile(path)
    try:
        # The header, from a query that matches nothing.
        await conn.copy_from_query(
            f"SELECT * FROM {table.name} WHERE false", output=out.write, format="csv", header=True
        )
        rows = 0
        if table.time is None:
            status = await conn.copy_from_query(
                f"SELECT * FROM {table.name} ORDER BY id", output=out.write, format="csv"
            )
            rows = _copied(status)
        elif table.key is None:
            rows = await _copy_sorted(
                conn,
                f"SELECT * FROM {table.name} WHERE {table.time} >= $1 AND {table.time} < $2 "
                f"ORDER BY {table.time}",
                table.bound(start),
                table.bound(end),
                output=out.write,
            )
        else:
            # One key at a time: each an index range scan, already in order, in
            # a short transaction of its own.
            for key in await table_keys(conn, table):
                rows += await _copy_sorted(
                    conn,
                    f"SELECT * FROM {table.name} WHERE {table.key} = $1 "
                    f"AND {table.time} >= $2 AND {table.time} < $3 ORDER BY {table.time}",
                    key,
                    table.bound(start),
                    table.bound(end),
                    output=out.write,
                )
    except BaseException:
        out.discard()
        raise
    await out.close()
    return rows


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    path = directory / MANIFEST
    with path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(directory)


def _read_manifest(directory: Path) -> dict[str, Any] | None:
    try:
        manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def location_of(path: Path) -> str:
    """Where a week is recorded as written: this host's name and the path on it."""
    return f"{socket.gethostname()}:{path}"


async def _record(conn: Any, start: int, location: Path, rows: dict[str, int], now: int) -> None:
    await conn.execute(
        "INSERT INTO openinfra_archives (week_start, location, tables, archived_at) "
        "VALUES ($1, $2, $3::jsonb, $4) ON CONFLICT (week_start) DO NOTHING",
        start,
        location_of(location),
        json.dumps(rows, sort_keys=True),
        now,
    )


async def archive_week(conn: Any, directory: Path, start: int, now: int) -> dict[str, int]:
    """Write the week starting at *start* under *directory* and record it; rows per table.

    A complete directory for the week left by a run that stopped before
    recording it is recorded as it is.

    Raises:
        ArchiveError: The week's directory exists but holds another archive.
        OSError: The directory cannot be written.
    """
    end = start + WEEK_MS
    name = week_name(start)
    final = directory / name
    if final.exists():
        manifest = _read_manifest(final)
        if not manifest or manifest.get("week_start_ms") != start:
            raise ArchiveError(f"{final} exists but is not the archive of the week of {name}")
        rows = {table: entry["rows"] for table, entry in manifest["tables"].items()}
        await _record(conn, start, final, rows, now)
        return rows

    partial = directory / f".{name}.partial"
    if partial.exists():
        # Left by a run that stopped part way; nothing refers to it.
        await asyncio.to_thread(shutil.rmtree, partial)
    await asyncio.to_thread(partial.mkdir, parents=True)
    rows: dict[str, int] = {}
    files: dict[str, dict[str, Any]] = {}
    try:

        async def export(table: Table) -> None:
            path = partial / f"{table.name}.csv.gz"
            rows[table.name] = await _export(conn, table, path, start, end)
            files[table.name] = {"file": path.name, "rows": rows[table.name]}
            files[table.name]["bytes"] = path.stat().st_size

        # An ended week no longer changes, so its tables need no common snapshot.
        for table in TABLES:
            if table.time is not None:
                await export(table)
        # The live host, GPU and engine rows do: read together, they agree.
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            for table in TABLES:
                if table.time is None:
                    await export(table)
        manifest = {
            "format": FORMAT,
            "week_start": _iso(start),
            "week_end": _iso(end),
            "week_start_ms": start,
            "week_end_ms": end,
            "archived_at": _iso(now),
            "tables": files,
            "notes": (
                "CSV with a header row. Times are milliseconds since the Unix epoch; a "
                "sample's bucket is its ts divided by 1000. Rollups are the week's 20-minute "
                "(grain 1200000) and hourly (grain 3600000) buckets. Hosts, GPUs and engines "
                "are their rows when the week was archived."
            ),
        }
        await asyncio.to_thread(_write_manifest, partial, manifest)
        await asyncio.to_thread(os.rename, partial, final)
        await asyncio.to_thread(_fsync_directory, directory)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, partial, True)
        raise
    await _record(conn, start, final, rows, now)
    return rows


async def archive_ended_weeks(
    conn: Any, directory: Path, now: int, *, limit: int = WEEKS_PER_RUN
) -> list[int]:
    """Archive every ended week with rows that is not archived yet, oldest first; their starts.

    At most *limit* weeks per call. Goes from one week with rows to the next,
    so a long gap costs nothing, and stops at the first week that fails, so
    the weeks archived are always the oldest ones.
    """
    done = await archived_weeks(conn)
    last = last_archivable_week(now)
    written: list[int] = []
    found = await oldest_time(conn)
    while found is not None and len(written) < limit:
        start = week_start(found)
        if start > last:
            break
        if start not in done:
            if not written:
                await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            rows = await archive_week(conn, directory, start, now)
            logger.info(
                "openinfra archive: week %s written to %s (%d rows)",
                week_name(start),
                directory / week_name(start),
                sum(rows.values()),
            )
            written.append(start)
        found = await next_time(conn, start + WEEK_MS)
    return written


# --- retention -------------------------------------------------------------------


async def _delete(conn: Any, table: Table, keys: list[str], lower: int, upper: int) -> int:
    """Delete *table*'s rows in [lower, upper) (milliseconds), in batches; how many."""
    if not keys or upper <= lower:
        return 0
    deleted = 0
    while True:
        status = await conn.execute(
            f"DELETE FROM {table.name} WHERE ctid = ANY(ARRAY("
            f"SELECT ctid FROM {table.name} WHERE {table.key} = ANY($1::text[]) "
            f"AND {table.time} >= $2 AND {table.time} < $3 LIMIT $4))",
            keys,
            table.bound(lower),
            table.bound(upper),
            DELETE_BATCH,
        )
        count = int(status.split()[-1])
        deleted += count
        if count < DELETE_BATCH:
            return deleted


async def prune(conn: Any, now: int, days: int, *, archived_only: bool) -> dict[str, int]:
    """Delete samples and request events older than *days*; rows deleted per table.

    With *archived_only*, only rows in archived weeks are deleted. Nothing is
    deleted when *days* is 0.
    """
    counts = {table.name: 0 for table in PRUNED}
    if days <= 0:
        return counts
    cutoff = now - days * DAY_MS
    oldest = await oldest_time(conn)
    if oldest is None or oldest >= cutoff:
        return counts
    if archived_only:
        weeks = [
            (start, min(start + WEEK_MS, cutoff))
            for start in await archived_weeks(conn)
            if start < cutoff and start + WEEK_MS > oldest
        ]
    else:
        weeks = [(oldest, cutoff)]
    for table in PRUNED:
        keys = await table_keys(conn, table)
        for lower, upper in weeks:
            counts[table.name] += await _delete(conn, table, keys, lower, upper)
    return counts


# --- command line ------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m serving.openinfra.archive",
        description=(
            "List the weeks of OpenInfra data archived so far, or archive the ended weeks "
            "and delete what is past the retention, as the scheduler does."
        ),
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help=(
            "refresh the rollups, archive and apply the retention once, as the scheduler does "
            "(OPENINFRA_ARCHIVE_DIR, OPENINFRA_RETENTION_DAYS, OPENINFRA_DELETE_UNARCHIVED)"
        ),
    )
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    import asyncpg

    from serving.config.settings import get_settings
    from serving.openinfra import config
    from serving.openinfra.dashboard import now_ms
    from serving.openinfra.maintenance import run_archive, run_maintenance

    settings = get_settings()
    pool = await asyncpg.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        min_size=1,
        max_size=1,
        server_settings={"application_name": "openinfra_archive"},
    )
    try:
        if args.run:
            # The rollups first, then the archive, then the retention again for
            # what was just archived.
            ran = (
                await run_maintenance(pool, now_ms())
                and await run_archive(pool, now_ms()) is not None
                and await run_maintenance(pool, now_ms())
            )
            if not ran:
                print("Another run holds a lock; try again shortly.")
                return 1
        async with pool.acquire() as conn:
            weeks = await archived_weeks(conn)
    finally:
        await pool.close()
    directory = config.archive_dir()
    if directory is not None:
        print(f"Archive directory: {directory}")
    elif config.delete_unarchived():
        print("Archive directory: none; rows past the retention are deleted unarchived")
    else:
        print("Archive directory: none; nothing is archived or deleted")
    days = config.retention_days()
    print(f"Retention: {f'{days} days' if days else 'none (everything is kept)'}")
    if not weeks:
        print("No week archived yet.")
    for start, week in weeks.items():
        print(f"{week_name(start)}  {sum(week['tables'].values()):>12} rows  {week['location']}")
    return 0


def cli(argv: Sequence[str] | None = None) -> int:
    """``python -m serving.openinfra.archive``."""
    from serving.config.manage import load_stored_configuration

    args = _parse_args(argv)
    # The retention is a stored setting; without this the environment's would apply.
    load_stored_configuration()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(cli())
