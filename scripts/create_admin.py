#!/usr/bin/env python3
"""Create or promote a user to admin tier in the hybridInference database.

Usage:
    python scripts/create_admin.py
    python scripts/create_admin.py --email admin@localhost --username admin --password test

Run from the project root so that the serving package is importable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from dotenv import load_dotenv

from serving.utils.jwt import generate_ulid
from serving.utils.password import hash_password


async def _run(email: str, username: str, password: str, db_config: dict) -> None:
    pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=2, command_timeout=30)
    try:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, email, role FROM users WHERE email = $1 OR user_name = $2",
                email,
                username,
            )

            if existing:
                user_id = existing["id"]
                if existing["role"] == "admin":
                    print(
                        f"User already exists with admin role: {existing['email']} (id={user_id})"
                    )
                    return
                await conn.execute(
                    "UPDATE users SET role = 'admin', status = 'active', email_verified = TRUE WHERE id = $1",
                    user_id,
                )
                print(f"Promoted existing user to admin: {existing['email']} (id={user_id})")
                return

            user_id = generate_ulid()
            password_hash = hash_password(password)
            await conn.execute(
                """
                INSERT INTO users (id, email, password_hash, user_name, role, status, email_verified)
                VALUES ($1, $2, $3, $4, 'admin', 'active', TRUE)
                """,
                user_id,
                email,
                password_hash,
                username,
            )
            print(f"Created admin user '{username}' <{email}> (id={user_id})")
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or promote a user to admin tier")
    parser.add_argument(
        "--email", default="admin@admin.com", help="Email address (default: admin@admin.com)"
    )
    parser.add_argument(
        "--username", default="admin@admin.com", help="Username (default: admin@admin.com)"
    )
    parser.add_argument("--password", default="admin", help="Password (default: admin)")
    args = parser.parse_args()

    load_dotenv()

    db_user = os.getenv("DB_USER")
    db_password_env = os.getenv("DB_PASSWORD")
    db_name = os.getenv("DB_NAME")

    if not db_user or not db_password_env or not db_name:
        print("Error: DB_USER, DB_PASSWORD, and DB_NAME must be set in .env", file=sys.stderr)
        sys.exit(1)

    db_config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "database": db_name,
        "user": db_user,
        "password": db_password_env,
    }

    asyncio.run(_run(args.email, args.username, args.password, db_config))


if __name__ == "__main__":
    main()
