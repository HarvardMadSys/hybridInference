"""Export api_logs table to a zstd-compressed JSONL file."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

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


async def _export_jsonl(pool: asyncpg.Pool, output_path: str) -> int:
    count = 0
    cctx = zstd.ZstdCompressor()
    with open(output_path, "wb") as raw, cctx.stream_writer(raw) as f:
        async with pool.acquire() as conn:
            async with conn.transaction():
                async for row in conn.cursor("SELECT * FROM api_logs ORDER BY id"):
                    f.write((json.dumps(dict(row), default=_convert) + "\n").encode())
                    count += 1
    return count


async def main(output_path: str = "api_logs_export.jsonl.zst") -> int:
    """Connect to Postgres and export api_logs to a zstd-compressed JSONL file."""
    db_user = os.environ.get("DB_USER")
    db_password = os.environ.get("DB_PASSWORD", "")
    if not db_user:
        print(
            "ERROR: DB_USER is not set. Load the .env file or set the environment variable.",
            flush=True,
        )
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
        count = await _export_jsonl(pool, output_path)
        if count == 0:
            print("No rows found in api_logs.")
            return 0
        print(f"Exported {count} rows to {output_path}")
        return 0
    finally:
        await pool.close()


def cli() -> None:
    """Parse CLI arguments and run the export."""
    parser = argparse.ArgumentParser(description="Export all api_logs rows to a JSONL file")
    parser.add_argument(
        "-o",
        "--output",
        default="api_logs_export.jsonl.zst",
        help="Output path for zstd-compressed JSONL (default: api_logs_export.jsonl.zst)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file (default: auto-detect /srv/hybridInference/.env or repo .env)",
    )
    args = parser.parse_args()
    _load_env(args.env_file)
    ret = asyncio.run(main(output_path=args.output))
    if ret != 0:
        exit(ret)


if __name__ == "__main__":
    cli()
