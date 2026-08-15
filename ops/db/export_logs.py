"""Export api_logs table to a zstd-compressed JSONL file."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

import asyncpg
import dotenv
import zstandard as zstd


def _load_env(env_path: str | None = None) -> None:
    candidates = [
        env_path,
        os.environ.get("ENV_FILE"),
        "/srv/hybridInference/.env",
        str(Path(__file__).resolve().parents[2] / ".env"),
    ]
    for p in candidates:
        if p and Path(p).is_file():
            dotenv.load_dotenv(p, override=False)
            return


def _convert(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


def _log(msg: str, stream: TextIO = sys.stderr) -> None:
    print(msg, file=stream, flush=True)


def parse_bound(value: str | None) -> datetime | None:
    """Parse a ``--since`` / ``--until`` value as UTC.

    A bare ``YYYY-MM-DD`` is midnight UTC that day. A timezone-naive
    datetime is treated as UTC.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        day = date.fromisoformat(text)
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def previous_iso_week(today: date | None = None) -> tuple[date, date]:
    """Return the previous complete ISO week as ``(monday, sunday)`` inclusive."""
    today = today or datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.isoweekday() - 1)
    start = this_monday - timedelta(days=7)
    end = this_monday - timedelta(days=1)
    return start, end


def build_select_sql(since: datetime | None, until: datetime | None) -> tuple[str, list[datetime]]:
    """Build a parameterized ``SELECT`` over ``api_logs``.

    ``since`` is inclusive, ``until`` is exclusive. Either bound may be omitted.
    """
    clauses: list[str] = []
    args: list[datetime] = []
    if since is not None:
        args.append(since)
        clauses.append(f"timestamp >= ${len(args)}")
    if until is not None:
        args.append(until)
        clauses.append(f"timestamp < ${len(args)}")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return f"SELECT * FROM api_logs{where} ORDER BY id", args


async def _export_jsonl(
    pool: asyncpg.Pool,
    output_path: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> int:
    query, args = build_select_sql(since, until)
    count = 0
    cctx = zstd.ZstdCompressor()
    with ExitStack() as stack:
        raw = (
            sys.stdout.buffer
            if output_path == "-"
            else stack.enter_context(open(output_path, "wb"))
        )
        f = stack.enter_context(cctx.stream_writer(raw))
        async with pool.acquire() as conn, conn.transaction():
            async for row in conn.cursor(query, *args):
                f.write((json.dumps(dict(row), default=_convert) + "\n").encode())
                count += 1
    return count


async def main(
    output_path: str = "api_logs_export.jsonl.zst",
    since: datetime | None = None,
    until: datetime | None = None,
) -> int:
    """Connect to Postgres and export api_logs to a zstd-compressed JSONL file."""
    if since is not None and until is not None and until <= since:
        _log("ERROR: --until must be after --since.")
        return 1
    db_user = os.environ.get("DB_USER")
    db_password = os.environ.get("DB_PASSWORD", "")
    if not db_user:
        _log("ERROR: DB_USER is not set. Load the .env file or set the environment variable.")
        return 1
    dsn = (
        f"postgresql://{db_user}"
        f":{db_password}"
        f"@{os.environ.get('DB_HOST', 'localhost')}"
        f":{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'hybridinference')}"
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        count = await _export_jsonl(pool, output_path, since=since, until=until)
        dest = "stdout" if output_path == "-" else output_path
        if count == 0:
            _log(f"No rows found in api_logs for this window; wrote empty archive to {dest}.")
            return 0
        _log(f"Exported {count} rows to {dest}")
        return 0
    finally:
        await pool.close()


def cli(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and run the export."""
    parser = argparse.ArgumentParser(
        description="Export api_logs rows to a zstd-compressed JSONL file"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="api_logs_export.jsonl.zst",
        help="Output path, or '-' for stdout (default: api_logs_export.jsonl.zst)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Inclusive lower bound (YYYY-MM-DD or ISO datetime). Default: all rows",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Exclusive upper bound (YYYY-MM-DD or ISO datetime). Default: no upper bound",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file (default: auto-detect /srv/hybridInference/.env or repo .env)",
    )
    args = parser.parse_args(argv)
    try:
        since = parse_bound(args.since)
        until = parse_bound(args.until)
    except ValueError as exc:
        _log(f"ERROR: invalid --since/--until: {exc}")
        return 1
    _load_env(args.env_file)
    return asyncio.run(main(output_path=args.output, since=since, until=until))


if __name__ == "__main__":
    raise SystemExit(cli())
