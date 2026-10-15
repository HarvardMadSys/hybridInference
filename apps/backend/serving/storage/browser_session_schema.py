"""Tables for browser sessions: one browser, the accounts signed in on it.

Both boot-time schema builders (``DatabaseLogger`` and
``PostgresOperationalStore``) create the auth tables, so they call this one
function rather than each carrying a copy that can drift.

One ``browser_sessions`` row per browser, keyed by the hash of the cookie it
holds. Its accounts are ``browser_session_accounts`` rows, each pointing at that
account's own ``auth_sessions`` row. These are new tables rather than a column
on ``auth_sessions``, so no boot alters a table the refresh path reads, and they
have no foreign keys, which would lock ``users`` or ``auth_sessions`` while
being created.
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
