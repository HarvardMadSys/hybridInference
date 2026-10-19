"""The last computed result of each slow admin view, kept in the database.

The Providers tab's performance distributions and TTFT scatter are computed in
the background and served from memory (``routers.admin._refreshing_cache``).
Memory does not survive a restart, and recomputing takes seconds to minutes, so
each new result is also written here and read back by a process that has none:
after a deploy the views show the previous result, with its age, at once.

A row holds the response as served (JSON), when it was computed, the
``definition`` of the computation that produced it, and the generation it was
computed under. A row under another definition -- computed by a revision whose
query or response shape differs -- is not served.

The results are aggregates over ``api_logs``, and the scatter keeps individual
requests' timestamps and sizes, so a user's hard delete must not leave them
behind -- including a result a computation that read the rows before the delete
publishes after it. So ``admin_view_state`` holds one generation:

- the delete advances it and drops every stored result, in its own transaction
  (:func:`clear`);
- a computation reads it before reading any row (:func:`generation`), and its
  :func:`write` stores the result only if it is unchanged, under a share lock on
  the generation's row: a delete that committed first is seen, and one that
  comes later waits for the write and then drops it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg


async def generation(pool: asyncpg.Pool) -> int:
    """The current generation: how many deletes have dropped the stored results."""
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT generation FROM admin_view_state")
    return int(value or 0)


async def read(pool: asyncpg.Pool, name: str, definition: str) -> tuple[dict[str, Any], int] | None:
    """View *name*'s stored payload and generation, if computed under *definition*."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload, generation FROM admin_view_results "
            "WHERE name = $1 AND definition = $2",
            name,
            definition,
        )
    return (json.loads(row["payload"]), int(row["generation"])) if row is not None else None


async def write(
    pool: asyncpg.Pool,
    name: str,
    definition: str,
    payload: dict[str, Any],
    computed_at: datetime,
    generation: int,
) -> bool:
    """Store *payload* as view *name*'s result if *generation* is still current.

    Returns whether the generation was current. A newer stored result (by
    ``computed_at``, under the same definition) is kept: two processes
    computing the view at once must not leave the older result behind.
    """
    async with pool.acquire() as conn, conn.transaction():
        current = await conn.fetchval("SELECT generation FROM admin_view_state FOR SHARE")
        if int(current or 0) != generation:
            return False
        await conn.execute(
            """
            INSERT INTO admin_view_results AS r
                (name, definition, payload, computed_at, generation)
            VALUES ($1, $2, $3::jsonb, $4, $5)
            ON CONFLICT (name) DO UPDATE SET
                definition = EXCLUDED.definition,
                payload = EXCLUDED.payload,
                computed_at = EXCLUDED.computed_at,
                generation = EXCLUDED.generation
            WHERE r.computed_at <= EXCLUDED.computed_at OR r.definition <> EXCLUDED.definition
            """,
            name,
            definition,
            json.dumps(payload),
            computed_at,
            generation,
        )
    return True


async def clear(conn: asyncpg.Connection) -> None:
    """Advance the generation and drop every stored result, in the caller's transaction.

    The generation first: its row lock is what orders this against a
    concurrent :func:`write`.
    """
    await conn.execute(
        "INSERT INTO admin_view_state (id, generation) VALUES (TRUE, 1) "
        "ON CONFLICT (id) DO UPDATE SET generation = admin_view_state.generation + 1"
    )
    await conn.execute("DELETE FROM admin_view_results")
