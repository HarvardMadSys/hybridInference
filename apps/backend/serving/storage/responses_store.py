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

When no database is configured the store is simply absent (``None`` in
``AppServices``) and statefulness degrades gracefully: ``store`` becomes a
no-op and ``previous_response_id`` / ``GET`` return 404.
"""

from __future__ import annotations

import json
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)


class ResponseStore:
    """Postgres-backed store for Responses API objects."""

    def __init__(self, pool: Any) -> None:
        """Wrap an asyncpg pool."""
        self._pool = pool

    async def initialize(self) -> None:
        """Create the ``openai_responses`` table and indexes (idempotent)."""
        async with self._pool.acquire() as conn:
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
        ``messages``, ``previous_response_id``, ``model`` — or ``None`` if not
        found. JSON columns are decoded to Python objects.
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


def _load_json(value: Any) -> Any:
    """Decode a JSONB column that asyncpg may hand back as str or object."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return value
