"""Persistence for OpenAI Responses API objects.

The Responses API is stateful: when ``store`` is true (the default) a response
is saved and can be fetched via ``GET /v1/responses/{id}`` or chained into a
follow-up request via ``previous_response_id``. This is a small, self-contained
store over the same Postgres pool used by the rest of the gateway — kept
separate from the ``OperationalStore`` ABC so the (cached, auth-hot) operational
contract is untouched.

Two JSON columns are kept per row:

- ``response`` — the full Responses object returned to the client (served
  verbatim by ``GET``).
- ``messages`` — the cumulative chat-completions message list **including** this
  turn's assistant output and **excluding** any system/instructions message, so
  a ``previous_response_id`` follow-up can replay the conversation in O(1)
  without walking a chain.

Each row holds the whole conversation so far, so a chain of turns stores its
early turns again in every later row. Two limits bound that:

- A stored response is kept for :data:`RETENTION_DAYS` (30) days from when it
  was created, as OpenAI keeps them by default, then purged by an hourly job
  (:func:`register_retention_job`). A chain still being continued is unaffected:
  its newest row carries the whole conversation.
- A conversation is continued with ``store`` on only while its stored
  ``messages`` are no larger than a request body may be
  (``serving.servers.routers.responses``), so no row grows past that by more
  than one turn.

When no database is configured the store is simply absent (``None`` in
``AppServices``) and statefulness degrades gracefully: ``store`` becomes a
no-op and ``previous_response_id`` / ``GET`` return 404.
"""

from __future__ import annotations

import json
import time
from datetime import UTC
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: How long a stored response is kept, from when it was created: the Responses
#: API's default retention.
RETENTION_DAYS = 30

#: Responses deleted per statement by :func:`purge_expired`.
PURGE_BATCH = 1000

# Up to ``$2`` responses created more than ``$1`` days ago. The table has no
# index on ``created_at`` alone, and ``idx_openai_responses_user`` is
# ``(user_id, created_at DESC)``: the recursive part walks that index from one
# owner to the next (a loose index scan), and each owner's expired responses are
# a range of it. So a run reads the index, once per owner, and the rows it
# deletes -- never the whole table, whose rows carry whole conversations.
_PURGE_SQL = """
DELETE FROM openai_responses
WHERE id = ANY(ARRAY(
    WITH RECURSIVE owners AS (
        (SELECT user_id FROM openai_responses ORDER BY user_id LIMIT 1)
        UNION ALL
        SELECT (
            SELECT r.user_id FROM openai_responses AS r
            WHERE r.user_id > owners.user_id
            ORDER BY r.user_id
            LIMIT 1
        )
        FROM owners
        WHERE owners.user_id IS NOT NULL
    )
    SELECT expired.id
    FROM owners
    CROSS JOIN LATERAL (
        SELECT r.id FROM openai_responses AS r
        WHERE r.user_id = owners.user_id
          AND r.created_at < now() - make_interval(days => $1)
        LIMIT $2
    ) AS expired
    LIMIT $2
))
"""

#: Minute past the hour the purge runs, clear of the rollups (5, 10, 15, 20, 25).
_PURGE_MINUTE = 40


class ResponseStore:
    """Postgres-backed store for Responses API objects."""

    def __init__(self, pool: Any, *, persist_enabled: bool = True) -> None:
        """Wrap an asyncpg pool.

        ``persist_enabled`` gates *writes* only: when False (DB privacy mode)
        :meth:`save` is a no-op, but reads, deletes and the hard-delete purge
        still operate so existing rows remain retrievable and wipeable.
        """
        self._pool = pool
        self.persist_enabled = persist_enabled

    async def save(
        self,
        *,
        response_id: str,
        user_id: str,
        response: dict[str, Any],
        messages: list[dict[str, Any]],
        previous_response_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Persist (or replace) a response and its cumulative conversation."""
        if not self.persist_enabled:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO openai_responses
                    (id, user_id, response, messages, previous_response_id, model)
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
                ON CONFLICT (id) DO UPDATE SET
                    response = EXCLUDED.response,
                    messages = EXCLUDED.messages,
                    previous_response_id = EXCLUDED.previous_response_id,
                    model = EXCLUDED.model
                """,
                response_id,
                user_id,
                json.dumps(response),
                json.dumps(messages),
                previous_response_id,
                model,
            )

    async def get(self, response_id: str) -> dict[str, Any] | None:
        """Fetch a stored response row by id.

        Returns a dict with keys ``id``, ``user_id``, ``response``,
        ``messages``, ``previous_response_id``, ``model`` and ``messages_bytes``
        -- or ``None`` if not found. JSON columns are decoded to Python objects;
        ``messages_bytes`` is the size of the stored conversation as JSON text.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, response, messages, previous_response_id, model "
                "FROM openai_responses WHERE id = $1",
                response_id,
            )
        if row is None:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "response": _load_json(row["response"]),
            "messages": _load_json(row["messages"]) or [],
            "previous_response_id": row["previous_response_id"],
            "model": row["model"],
            "messages_bytes": _json_bytes(row["messages"]),
        }

    async def delete(self, response_id: str, *, user_id: str | None = None) -> bool:
        """Delete a stored response. Returns True when a row was removed.

        When ``user_id`` is given the delete is scoped to that owner so one
        user cannot delete another's response.
        """
        async with self._pool.acquire() as conn:
            if user_id is None:
                result = await conn.execute(
                    "DELETE FROM openai_responses WHERE id = $1", response_id
                )
            else:
                result = await conn.execute(
                    "DELETE FROM openai_responses WHERE id = $1 AND user_id = $2",
                    response_id,
                    user_id,
                )
        try:
            return int(result.rsplit(" ", 1)[-1]) > 0
        except (ValueError, AttributeError):
            return False

    async def delete_user_responses(self, user_id: str) -> int:
        """Delete every stored response owned by a user; return the row count.

        Used by the admin hard-delete flow to purge a user's conversation data
        (full ``response`` + cumulative ``messages`` JSONB) from this table,
        which is owned neither by the ``OperationalStore`` nor the ``LogStore``.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM openai_responses WHERE user_id = $1", user_id)
        try:
            return int(result.rsplit(" ", 1)[-1])
        except (ValueError, AttributeError):
            return 0


async def purge_expired(
    pool: Any, *, retention_days: int = RETENTION_DAYS, batch: int = PURGE_BATCH
) -> int:
    """Delete the responses created more than *retention_days* ago; return how many.

    In statements of at most *batch* rows, until one finds fewer, so none holds
    many locks or runs long; each takes a pooled connection only for itself.
    """
    purged = 0
    while True:
        async with pool.acquire() as conn:
            deleted = _rowcount(await conn.execute(_PURGE_SQL, retention_days, batch))
        purged += deleted
        if deleted < batch:
            return purged


async def retention_job(pool: Any) -> None:
    """The hourly purge, for the scheduler: never raises."""
    started_at = time.monotonic()
    purged = 0
    outcome = "error"
    try:
        purged = await purge_expired(pool)
        outcome = "ok"
    except Exception:
        logger.exception("responses_retention failed")
    finally:
        logger.info(
            "responses_retention: purged=%d duration_ms=%d outcome=%s",
            purged,
            int((time.monotonic() - started_at) * 1000),
            outcome,
        )


def register_retention_job(scheduler: Any, pool: Any) -> None:
    """Register the hourly purge of expired responses on the existing AsyncIOScheduler."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        retention_job,
        trigger=CronTrigger(minute=_PURGE_MINUTE, timezone=UTC),
        args=[pool],
        id="responses_retention",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    logger.info("responses_retention: registered on scheduler (cron minute=%d)", _PURGE_MINUTE)


def _rowcount(status: Any) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


def _json_bytes(value: Any) -> int:
    """The size of a JSON column as UTF-8 text, however asyncpg handed it back."""
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode())
    return len(json.dumps(value).encode()) if value is not None else 0


def _load_json(value: Any) -> Any:
    """Decode a JSONB column that asyncpg may hand back as str or object."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            # Invalid JSON in a JSONB column signals data corruption (manual
            # edit / migration bug); surface it rather than silently dropping.
            logger.warning("Failed to decode openai_responses JSON column", exc_info=True)
            return None
    return value
