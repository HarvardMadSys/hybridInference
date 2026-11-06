"""The ``openai_responses`` table, frozen.

Copied from ``serving.storage.responses_store``.
"""

from __future__ import annotations

from typing import Any


async def create_tables(conn: Any) -> None:
    """Create the ``openai_responses`` table and indexes (idempotent)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS openai_responses (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            response JSONB NOT NULL,
            messages JSONB NOT NULL,
            previous_response_id TEXT,
            model TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_openai_responses_user "
        "ON openai_responses(user_id, created_at DESC)"
    )
