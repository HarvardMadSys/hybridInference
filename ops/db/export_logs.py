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
    with open(output_path, "w") as f:
        async with pool.acquire() as conn:
            async with conn.transaction():
                async for row in conn.cursor("SELECT * FROM api_logs ORDER BY id"):
                    f.write(json.dumps(dict(row), default=_convert) + "\n")
                    count += 1
    return count


async def main(output_path: str = "api_logs_export.jsonl") -> None:
    dsn = (
        f"postgresql://{os.environ.get('DB_USER', 'postgres')}"
        f":{os.environ.get('DB_PASSWORD', '')}"
        f"@{os.environ.get('DB_HOST', 'localhost')}"
        f":{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'freeinference_db')}"
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        count = await _export_jsonl(pool, output_path)
        if count == 0:
            print("No rows found in api_logs.")
            return
        print(f"Exported {count} rows to {output_path}")
    finally:
        await pool.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Export all api_logs rows to a JSONL file")
    parser.add_argument(
        "-o",
        "--output",
        default="api_logs_export.jsonl",
        help="Output JSONL file path (default: api_logs_export.jsonl)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file (default: auto-detect /srv/hybridInference/.env or repo .env)",
    )
    args = parser.parse_args()
    _load_env(args.env_file)
    asyncio.run(main(output_path=args.output))


if __name__ == "__main__":
    cli()
