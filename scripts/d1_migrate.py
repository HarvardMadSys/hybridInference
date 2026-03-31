#!/usr/bin/env python3
"""Migrate operational tables from PostgreSQL to Cloudflare D1.

Copies the 6 operational tables (users, api_keys, auth_sessions,
email_verification_tokens, password_reset_tokens, admin_audit_log)
from PostgreSQL to D1 via the REST API.

Usage:
    # Dry run (shows what would be migrated)
    python scripts/d1_migrate.py --dry-run

    # Full migration
    python scripts/d1_migrate.py

    # Resume from checkpoint
    python scripts/d1_migrate.py --resume

    # Custom batch size
    python scripts/d1_migrate.py --batch-size 50

    # Validate only (compare row counts + spot-check)
    python scripts/d1_migrate.py --validate-only

Requirements:
    - PostgreSQL must be running and accessible (DB_* env vars)
    - D1 credentials must be set (D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN)
    - D1 schema must already be initialized (run with DB_BACKEND=d1 first,
      or manually execute d1_schema.sql)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Table definitions: column names, primary key, and type transforms
# ---------------------------------------------------------------------------

_TABLES: list[dict[str, Any]] = [
    {
        "name": "users",
        "pk": "id",
        "order_by": "created_at",
        "columns": [
            "id",
            "email",
            "password_hash",
            "user_name",
            "preferences",
            "role",
            "email_verified",
            "status",
            "approval_note",
            "reviewed_at",
            "reviewed_by",
            "created_at",
            "last_login_at",
        ],
    },
    {
        "name": "api_keys",
        "pk": "id",
        "order_by": "id",
        "columns": [
            "id",
            "key_hash",
            "key_prefix",
            "user_id",
            "user_name",
            "status",
            "quota_daily_cost_usd",
            "quota_monthly_cost_usd",
            "created_at",
            "expires_at",
            "last_used_at",
            "tier",
            "notes",
            "metadata",
            "account_id",
        ],
    },
    {
        "name": "auth_sessions",
        "pk": "id",
        "order_by": "created_at",
        "columns": [
            "id",
            "user_id",
            "refresh_token_hash",
            "jti",
            "sid",
            "created_at",
            "last_used_at",
            "expires_at",
            "revoked",
            "user_agent",
            "ip_address",
        ],
    },
    {
        "name": "email_verification_tokens",
        "pk": "token",
        "order_by": "created_at",
        "columns": ["token", "user_id", "created_at", "expires_at", "used_at"],
    },
    {
        "name": "password_reset_tokens",
        "pk": "token",
        "order_by": "created_at",
        "columns": ["token", "user_id", "created_at", "expires_at", "used_at"],
    },
    {
        "name": "admin_audit_log",
        "pk": "id",
        "order_by": "id",
        "columns": [
            "id",
            "timestamp",
            "admin_ip",
            "action",
            "target_user_id",
            "details",
            "success",
        ],
    },
]

# Columns that need type transformation from Postgres → D1
_TIMESTAMP_COLUMNS = frozenset(
    {
        "created_at",
        "last_login_at",
        "expires_at",
        "last_used_at",
        "reviewed_at",
        "used_at",
        "timestamp",
    }
)
_BOOLEAN_COLUMNS = frozenset({"email_verified", "revoked", "success"})
_JSONB_COLUMNS = frozenset({"preferences", "metadata", "details"})
_DECIMAL_COLUMNS = frozenset({"quota_daily_cost_usd", "quota_monthly_cost_usd"})

# Checkpoint file for resumable migration
_CHECKPOINT_FILE = Path("data/d1_migration_checkpoint.json")


# ---------------------------------------------------------------------------
# Type transforms
# ---------------------------------------------------------------------------


def _transform_value(col: str, val: Any) -> Any:
    """Transform a single Postgres value to D1-compatible format."""
    if val is None:
        return None

    if col in _TIMESTAMP_COLUMNS:
        if isinstance(val, datetime):
            return val.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return str(val)

    if col in _BOOLEAN_COLUMNS:
        if isinstance(val, bool):
            return int(val)
        return int(bool(val))

    if col in _JSONB_COLUMNS:
        if isinstance(val, dict):
            return json.dumps(val)
        if isinstance(val, str):
            return val
        return json.dumps(val)

    if col in _DECIMAL_COLUMNS:
        if isinstance(val, Decimal):
            return float(val)
        return val

    return val


def _transform_row(columns: list[str], row: dict[str, Any]) -> list[Any]:
    """Transform a full row's values for D1 insertion."""
    return [_transform_value(col, row.get(col)) for col in columns]


# ---------------------------------------------------------------------------
# Checkpoint management
# ---------------------------------------------------------------------------


def _load_checkpoint() -> dict[str, Any]:
    """Load checkpoint state from file."""
    if _CHECKPOINT_FILE.exists():
        return json.loads(_CHECKPOINT_FILE.read_text())
    return {}


def _save_checkpoint(state: dict[str, Any]) -> None:
    """Save checkpoint state to file."""
    _CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Core migration logic
# ---------------------------------------------------------------------------


async def _get_pg_count(pool: Any, table: str) -> int:
    """Get row count from Postgres table."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT COUNT(*) as cnt FROM {table}")
    return row["cnt"]


async def _get_d1_count(d1: Any, table: str) -> int:
    """Get row count from D1 table."""
    result = await d1.query(f"SELECT COUNT(*) as cnt FROM {table}")
    return result.rows[0]["cnt"] if result.rows else 0


async def _fetch_pg_batch(
    pool: Any,
    table_def: dict[str, Any],
    offset: int,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Fetch a batch of rows from Postgres."""
    columns = ", ".join(table_def["columns"])
    order_by = table_def["order_by"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {columns} FROM {table_def['name']} ORDER BY {order_by} LIMIT $1 OFFSET $2",
            batch_size,
            offset,
        )
    return [dict(r) for r in rows]


def _build_insert_sql(table_def: dict[str, Any]) -> str:
    """Build an INSERT OR IGNORE statement for the table."""
    columns = table_def["columns"]
    col_list = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    return f"INSERT OR IGNORE INTO {table_def['name']} ({col_list}) VALUES ({placeholders})"


async def _migrate_table(
    pool: Any,
    d1: Any,
    table_def: dict[str, Any],
    batch_size: int,
    dry_run: bool,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Migrate a single table from Postgres to D1.

    Returns stats dict with rows_migrated, rows_skipped, duration_s.
    """
    table = table_def["name"]
    start = time.monotonic()

    pg_count = await _get_pg_count(pool, table)
    start_offset = checkpoint.get(table, {}).get("offset", 0)

    print(f"\n  {table}: {pg_count} rows in Postgres", end="")
    if start_offset > 0:
        print(f" (resuming from offset {start_offset})", end="")
    print()

    if dry_run:
        return {"rows_migrated": 0, "rows_skipped": 0, "pg_count": pg_count, "dry_run": True}

    insert_sql = _build_insert_sql(table_def)
    total_migrated = 0
    total_skipped = 0
    offset = start_offset

    while offset < pg_count:
        rows = await _fetch_pg_batch(pool, table_def, offset, batch_size)
        if not rows:
            break

        # Build batch of INSERT OR IGNORE statements
        statements: list[tuple[str, list[Any] | None]] = []
        for row in rows:
            params = _transform_row(table_def["columns"], row)
            statements.append((insert_sql, params))

        results = await d1.batch(statements)
        batch_inserted = sum(r.changes for r in results)
        batch_skipped = len(rows) - batch_inserted
        total_migrated += batch_inserted
        total_skipped += batch_skipped
        offset += len(rows)

        # Update checkpoint
        checkpoint[table] = {"offset": offset, "migrated": total_migrated}
        _save_checkpoint(checkpoint)

        print(
            f"    batch {offset}/{pg_count}: +{batch_inserted} inserted, "
            f"{batch_skipped} skipped (already exist)"
        )

    duration = time.monotonic() - start
    return {
        "rows_migrated": total_migrated,
        "rows_skipped": total_skipped,
        "pg_count": pg_count,
        "duration_s": round(duration, 2),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def _validate_table(
    pool: Any,
    d1: Any,
    table_def: dict[str, Any],
    sample_size: int = 5,
) -> dict[str, Any]:
    """Validate migration for a single table.

    Compares row counts and spot-checks sample rows.
    """
    table = table_def["name"]
    pk = table_def["pk"]

    pg_count = await _get_pg_count(pool, table)
    d1_count = await _get_d1_count(d1, table)

    count_match = pg_count == d1_count
    mismatches: list[str] = []

    # Spot-check: fetch sample PKs from Postgres, verify they exist in D1
    if pg_count > 0 and d1_count > 0:
        async with pool.acquire() as conn:
            sample_rows = await conn.fetch(
                f"SELECT {pk} FROM {table} ORDER BY {table_def['order_by']} LIMIT $1",
                sample_size,
            )

        for row in sample_rows:
            pk_val = row[pk]
            d1_result = await d1.query(
                f"SELECT {pk} FROM {table} WHERE {pk} = ?",
                [pk_val],
            )
            if not d1_result.rows:
                mismatches.append(f"{pk}={pk_val} missing in D1")

    status = "OK" if count_match and not mismatches else "MISMATCH"
    return {
        "table": table,
        "pg_count": pg_count,
        "d1_count": d1_count,
        "count_match": count_match,
        "mismatches": mismatches,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    """Execute the migration."""
    import asyncpg

    from serving.config.settings import get_settings
    from serving.storage.d1_client import D1Client

    get_settings.cache_clear()
    settings = get_settings()

    # Connect to Postgres
    print("Connecting to PostgreSQL...")
    try:
        pool = await asyncpg.create_pool(
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            min_size=1,
            max_size=5,
        )
    except Exception as exc:
        print(f"ERROR: Cannot connect to PostgreSQL: {exc}", file=sys.stderr)
        return 1

    # Connect to D1
    print("Connecting to Cloudflare D1...")
    if not all([settings.d1_account_id, settings.d1_database_id, settings.d1_api_token]):
        print(
            "ERROR: D1 credentials missing. Set D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN.",
            file=sys.stderr,
        )
        await pool.close()
        return 1

    d1 = D1Client(
        account_id=settings.d1_account_id,
        database_id=settings.d1_database_id,
        api_token=settings.d1_api_token,
    )

    if not await d1.health_check():
        print("ERROR: D1 health check failed.", file=sys.stderr)
        await d1.close()
        await pool.close()
        return 1

    print(
        f"Connected. Mode: {'dry-run' if args.dry_run else 'live'}, batch size: {args.batch_size}"
    )

    try:
        if args.validate_only:
            print("\n=== Validation ===")
            all_ok = True
            for table_def in _TABLES:
                result = await _validate_table(pool, d1, table_def)
                icon = "OK" if result["status"] == "OK" else "FAIL"
                print(
                    f"  [{icon}] {result['table']}: pg={result['pg_count']} d1={result['d1_count']}"
                )
                if result["mismatches"]:
                    for m in result["mismatches"]:
                        print(f"       {m}")
                    all_ok = False
            return 0 if all_ok else 1

        # Load or reset checkpoint
        checkpoint = _load_checkpoint() if args.resume else {}

        print("\n=== Migration ===")
        total_start = time.monotonic()
        all_stats: list[dict[str, Any]] = []

        for table_def in _TABLES:
            stats = await _migrate_table(
                pool, d1, table_def, args.batch_size, args.dry_run, checkpoint
            )
            all_stats.append({"table": table_def["name"], **stats})

        total_duration = time.monotonic() - total_start

        # Summary
        print("\n=== Summary ===")
        for s in all_stats:
            if s.get("dry_run"):
                print(f"  {s['table']}: {s['pg_count']} rows (dry run)")
            else:
                print(
                    f"  {s['table']}: {s['rows_migrated']} migrated, "
                    f"{s['rows_skipped']} skipped, {s.get('duration_s', 0)}s"
                )
        print(f"\n  Total time: {total_duration:.1f}s")

        # Post-migration validation
        if not args.dry_run:
            print("\n=== Post-migration validation ===")
            all_ok = True
            for table_def in _TABLES:
                result = await _validate_table(pool, d1, table_def)
                icon = "OK" if result["status"] == "OK" else "FAIL"
                print(
                    f"  [{icon}] {result['table']}: pg={result['pg_count']} d1={result['d1_count']}"
                )
                if result["mismatches"]:
                    for m in result["mismatches"]:
                        print(f"       {m}")
                    all_ok = False

            if all_ok:
                # Clean up checkpoint on success
                if _CHECKPOINT_FILE.exists():
                    _CHECKPOINT_FILE.unlink()
                print("\nMigration complete and validated.")
            else:
                print(
                    "\nWARNING: Validation found mismatches. "
                    "Re-run without --resume to retry full migration.",
                    file=sys.stderr,
                )
                return 1

    finally:
        await d1.close()
        await pool.close()

    return 0


def main() -> None:
    """Parse arguments and run the migration."""
    parser = argparse.ArgumentParser(
        description="Migrate operational tables from PostgreSQL to Cloudflare D1."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without writing to D1.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint instead of starting fresh.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate (compare row counts and spot-check), no migration.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of rows per D1 batch request (default: 50).",
    )
    args = parser.parse_args()

    if args.batch_size < 1 or args.batch_size > 100:
        parser.error("--batch-size must be between 1 and 100")

    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
