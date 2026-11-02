"""Tables for browser sessions: one browser, the accounts signed in on it. Frozen.

Copied from ``serving.storage.browser_session_schema``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


async def ensure_browser_session_tables(conn: asyncpg.Connection) -> None:
    """Create the browser-session tables and their index if missing."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS browser_sessions (
            id TEXT PRIMARY KEY,
            secret_hash TEXT NOT NULL UNIQUE,
            active_user_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS browser_session_accounts (
            browser_session_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            auth_session_id TEXT NOT NULL,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (browser_session_id, user_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_browser_session_accounts_user "
        "ON browser_session_accounts(user_id)"
    )
