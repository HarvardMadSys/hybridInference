#!/usr/bin/env python3
"""Export Cloudflare D1 operational tables to local JSON files.

Dumps each of the 6 operational tables to a timestamped JSON file,
suitable for disaster recovery or migration rollback.

Usage:
    # Export all tables
    python scripts/d1_backup.py

    # Export to custom directory
    python scripts/d1_backup.py --output-dir /tmp/d1-backup

    # Export a single table
    python scripts/d1_backup.py --table users

Requirements:
    - D1 credentials must be set (D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

load_dotenv()

_TABLES = [
    "users",
    "api_keys",
    "auth_sessions",
    "email_verification_tokens",
    "password_reset_tokens",
    "admin_audit_log",
]


async def _export_table(d1: Any, table: str) -> list[dict[str, Any]]:
    """Fetch all rows from a D1 table using paginated reads."""
    page_size = 1000
    offset = 0
    all_rows: list[dict[str, Any]] = []
    while True:
        result = await d1.query(f"SELECT * FROM {table} LIMIT ? OFFSET ?", [page_size, offset])
        if not result.rows:
            break
        all_rows.extend(result.rows)
        if len(result.rows) < page_size:
            break
        offset += page_size
    return all_rows


async def _run(args: argparse.Namespace) -> int:
    """Execute the backup."""
    from serving.config.settings import get_settings
    from serving.storage.d1_client import D1Client

    get_settings.cache_clear()
    settings = get_settings()

    if not all([settings.d1_account_id, settings.d1_database_id, settings.d1_api_token]):
        print(
            "ERROR: D1 credentials missing. Set D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN.",
            file=sys.stderr,
        )
        return 1

    d1 = D1Client(
        account_id=settings.d1_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.d1_api_token,
    )

    if not await d1.health_check():
        print("ERROR: D1 health check failed.", file=sys.stderr)
        await d1.close()
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    tables = [args.table] if args.table else _TABLES
    start = time.monotonic()

    try:
        for table in tables:
            rows = await _export_table(d1, table)
            out_path = output_dir / f"{table}_{timestamp}.json"
            out_path.write_text(json.dumps(rows, indent=2, default=str))
            print(f"  {table}: {len(rows)} rows → {out_path}")
    finally:
        await d1.close()

    duration = time.monotonic() - start
    print(f"\nBackup complete ({duration:.1f}s)")
    return 0


def main() -> None:
    """Parse arguments and run the backup."""
    parser = argparse.ArgumentParser(description="Export D1 operational tables to JSON.")
    parser.add_argument(
        "--output-dir",
        default="backups/d1",
        help="Directory for backup files (default: backups/d1).",
    )
    parser.add_argument(
        "--table",
        choices=_TABLES,
        help="Export a single table instead of all.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
