"""The last computed result of each slow admin view, kept in the database.

The Providers tab's performance distributions and TTFT scatter are computed in
the background and served from memory (``routers.admin._refreshing_cache``).
Memory does not survive a restart, and recomputing takes seconds to minutes, so
each new result is also written here and read back by a process that has none:
after a deploy the views show the previous result, with its age, at once.

A row holds the response as served (JSON), when it was computed, and the
``definition`` of the computation that produced it. A row under another
definition -- computed by a revision whose query or response shape differs --
is not served.

The results are aggregates over ``api_logs``, and the scatter keeps individual
requests' timestamps and sizes, so a user's hard delete drops them all
(:func:`clear`) in its own transaction; they are computed again on next view.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg


async def read(pool: asyncpg.Pool, name: str, definition: str) -> dict[str, Any] | None:
    """The stored payload of view *name*, if it was computed under *definition*."""
    async with pool.acquire() as conn:
        payload = await conn.fetchval(
            "SELECT payload FROM admin_view_results WHERE name = $1 AND definition = $2",
            name,
            definition,
        )
    return json.loads(payload) if payload is not None else None


async def write(
    pool: asyncpg.Pool,
    name: str,
    definition: str,
    payload: dict[str, Any],
    computed_at: datetime,
) -> None:
    """Store *payload* as view *name*'s result, unless a newer one is stored.

    Newer by ``computed_at``: two processes computing the view at once must not
    leave the older result behind.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_view_results AS r (name, definition, payload, computed_at)
            VALUES ($1, $2, $3::jsonb, $4)
            ON CONFLICT (name) DO UPDATE SET
                definition = EXCLUDED.definition,
                payload = EXCLUDED.payload,
                computed_at = EXCLUDED.computed_at
            WHERE r.computed_at <= EXCLUDED.computed_at OR r.definition <> EXCLUDED.definition
            """,
            name,
            definition,
            json.dumps(payload),
            computed_at,
        )


async def clear(conn: asyncpg.Connection) -> None:
    """Drop every stored result, in the caller's transaction."""
    await conn.execute("DELETE FROM admin_view_results")
