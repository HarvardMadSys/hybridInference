#!/usr/bin/env python3
"""Mark users with unverified email addresses as verified.

Usage:
    uv run python ops/admin/mark_unverified_emails_verified.py
    uv run python ops/admin/mark_unverified_emails_verified.py --apply

The default mode is a dry run. Pass ``--apply`` to update matching rows.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

UNVERIFIED_USERS_SQL = """
    SELECT id, email, user_name, role, status, created_at, last_login_at
    FROM users
    WHERE email_verified IS NOT TRUE
    ORDER BY created_at DESC
"""

UPDATE_UNVERIFIED_USERS_SQL = """
    UPDATE users
    SET email_verified = TRUE
    WHERE email_verified IS NOT TRUE
    RETURNING id, email, user_name, role, status, created_at, last_login_at
"""


async def _fetch_unverified(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return list(await conn.fetch(UNVERIFIED_USERS_SQL))


async def _mark_verified(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    async with conn.transaction():
        return list(await conn.fetch(UPDATE_UNVERIFIED_USERS_SQL))


def _print_rows(rows: list[asyncpg.Record], action: str) -> None:
    if not rows:
        print(f"No users {action}.")
        return

    print(f"{len(rows)} user(s) {action}:")
    for row in rows:
        last_login_at = row["last_login_at"] or ""
        print(
            f"- {row['email']} | username={row['user_name']} | "
            f"role={row['role']} | status={row['status']} | "
            f"created_at={row['created_at']} | last_login_at={last_login_at}"
        )


async def _run(db_config: dict[str, Any], apply_changes: bool) -> None:
    pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=2, command_timeout=30)
    try:
        async with pool.acquire() as conn:
            if apply_changes:
                rows = await _mark_verified(conn)
                _print_rows(rows, "updated")
                return

            rows = await _fetch_unverified(conn)
            _print_rows(rows, "would be updated")
            if rows:
                print("\nDry run only. Re-run with --apply to mark these users verified.")
    finally:
        await pool.close()


def _db_config_from_env() -> dict[str, Any]:
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    db_name = os.getenv("DB_NAME")

    if not db_user or not db_password or not db_name:
        print("Error: DB_USER, DB_PASSWORD, and DB_NAME must be set in .env", file=sys.stderr)
        sys.exit(1)

    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "database": db_name,
        "user": db_user,
        "password": db_password,
    }


def main() -> None:
    """Parse CLI arguments and mark unverified email addresses as verified."""
    parser = argparse.ArgumentParser(
        description="Mark every user where email_verified IS NOT TRUE as verified."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Update matching users. Without this flag, the script only prints a dry run.",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(Path.cwd() / ".env")
    asyncio.run(_run(_db_config_from_env(), args.apply))


if __name__ == "__main__":
    main()
