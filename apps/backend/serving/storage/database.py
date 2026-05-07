"""PostgreSQL-backed request/metrics logger using asyncpg.

This module provides a simple database logger that writes API requests,
responses, and usage metrics into PostgreSQL tables. It is intended for
production or staging environments where PostgreSQL is available.

Pure utility functions (``calculate_cost``, ``compute_prompt_hash``, etc.)
have been moved to ``serving.storage.utils`` so they can be imported without
pulling in asyncpg. They are re-exported here for backward compatibility.
"""

import json
from typing import Any

import asyncpg

from serving.storage.utils import (
    calculate_cost,
    compute_prompt_hash,
    compute_prompt_hash_chunked,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)


def _parse_admin_emails(raw: str) -> list[str]:
    """Return normalized admin email addresses from a comma-separated env var."""
    return [email.strip().lower() for email in raw.split(",") if email.strip()]


def _parse_command_tag_count(command_tag: str) -> int:
    """Extract the affected row count from an asyncpg command tag."""
    parts = command_tag.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


class DatabaseLogger:
    """Asynchronous PostgreSQL logger using a pooled connection."""

    def __init__(
        self,
        db_config: dict[str, str],
        store_full_prompts: bool = True,
        use_chunked_hash: bool = False,
    ):
        """Initialize the logger with a DSN/config mapping.

        Args:
            db_config: Mapping with asyncpg pool connection arguments.
            store_full_prompts: If False, only store prompt_hash for privacy.
                When disabled, prompt and response fields will be NULL and only
                hashes are stored for analytics.
            use_chunked_hash: If True, use 4-token chunked hashing instead of
                full prompt hashing. Both methods provide equivalent privacy
                protection but chunked hashing may enable future optimizations.
        """
        self.db_config = db_config
        self.store_full_prompts = store_full_prompts
        self.use_chunked_hash = use_chunked_hash
        # Use Any to avoid mypy issues when asyncpg types are unavailable.
        self.pool: Any | None = None

    async def initialize(self) -> None:
        """Open the asyncpg connection pool.

        Schema is owned by Alembic (see
        ``apps/backend/serving/storage/migrations``); the deploy pipeline
        runs ``alembic upgrade head`` before the gateway starts, and
        ``serving.servers.bootstrap._verify_schema_version`` hard-fails
        boot if the version doesn't match. We therefore do *not* run any
        CREATE TABLE / ALTER TABLE here.
        """
        self.pool = await asyncpg.create_pool(
            **self.db_config, min_size=2, max_size=10, command_timeout=60
        )

    async def log_request(
        self,
        request_id: str,
        model_id: str,
        provider: str,
        prompt: list[dict[str, Any]] | str,
        response: dict[str, Any] | str | None,
        usage: dict[str, int] | None,
        latency_ms: int,
        status_code: int,
        error: str | None = None,
        params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        ttft_ms: int | None = None,
        prompt_hash: str | None = None,
        response_hash: str | None = None,
        store_full_content: bool | None = None,
        pricing: dict[str, str] | None = None,
        upstream_cost_usd: float | None = None,
    ) -> None:
        """Insert a single request log row.

        Args:
            request_id: Unique request identifier.
            model_id: Logical model identifier.
            provider: Provider name for the request.
            prompt: Request messages payload (list of dicts or string).
            response: Provider response payload (dict, string, or None).
            usage: Token usage breakdown.
            latency_ms: End-to-end latency in milliseconds.
            status_code: HTTP status code returned to client.
            error: Optional error message.
            params: Request parameters (temperature, max_tokens, stream, etc.).
            metadata: Additional metadata (user_id, session_id, etc.).
            ttft_ms: Time to first token in milliseconds.
            prompt_hash: Hash of prompt for deduplication/caching (auto-computed if None).
            response_hash: Hash of response for deduplication/caching (auto-computed if None).
            store_full_content: Override instance default for storing full prompt/response.
                If None, uses self.store_full_prompts. Set False for privacy mode (hash only).
            pricing: Model pricing config for cost calculation (per 1M tokens).
            upstream_cost_usd: OpenRouter-reported per-request upstream cost (USD).
                Internal accounting only — orthogonal to user-billed `cost_usd`.
                None for non-OpenRouter routes.
        """
        if not self.pool:
            raise RuntimeError("DatabaseLogger not initialized")

        # Auto-compute prompt_hash if not provided
        if prompt_hash is None:
            if self.use_chunked_hash:
                prompt_hash = compute_prompt_hash_chunked(prompt)
            else:
                prompt_hash = compute_prompt_hash(prompt)

        # Auto-compute response_hash if not provided
        if response_hash is None and response is not None:
            # Normalize response to string for hashing
            # Use same normalization strategy as prompt for consistency
            response_for_hash = (
                json.dumps(response, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                if isinstance(response, dict)
                else str(response)
            )
            if self.use_chunked_hash:
                response_hash = compute_prompt_hash_chunked(response_for_hash)
            else:
                response_hash = compute_prompt_hash(response_for_hash)

        # Privacy control: use per-request override or instance default
        should_store_full = (
            store_full_content if store_full_content is not None else self.store_full_prompts
        )

        if should_store_full:
            prompt_to_store = prompt
            response_to_store = response
        else:
            prompt_to_store = None
            response_to_store = None

        # Extract parameters
        params = params or {}
        temperature = params.get("temperature")
        top_p = params.get("top_p")
        max_tokens = params.get("max_tokens")
        seed = params.get("seed")
        stream = params.get("stream", False)

        # Extract metadata
        metadata = metadata or {}
        user_id = metadata.get("user_id")
        session_id = metadata.get("session_id")
        tools = metadata.get("tools")

        # Token usage
        usage = usage or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        reasoning_tokens = usage.get("reasoning_tokens")
        total_tokens = usage.get("total_tokens")
        cache_read_tokens = usage.get("cache_read_tokens")
        cache_write_tokens = usage.get("cache_write_tokens")

        # Compute user-billed cost when pricing is available
        cost_usd = calculate_cost(usage if usage else None, pricing)

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO api_logs (
                    request_id, model_id, provider,
                    temperature, top_p, max_tokens, seed, stream,
                    ttft_ms, latency_ms,
                    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens,
                    cache_read_tokens, cache_write_tokens,
                    prompt, response, prompt_hash, response_hash,
                    status_code, error, user_id, session_id, metadata, tools,
                    cost_usd, upstream_cost_usd
                ) VALUES (
                    $1, $2, $3,
                    $4, $5, $6, $7, $8,
                    $9, $10,
                    $11, $12, $13, $14,
                    $15, $16,
                    $17, $18, $19, $20,
                    $21, $22, $23, $24, $25, $26,
                    $27, $28
                )
                """,
                request_id,
                model_id,
                provider,
                temperature,
                top_p,
                max_tokens,
                seed,
                stream,
                ttft_ms,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                reasoning_tokens,
                total_tokens,
                cache_read_tokens,
                cache_write_tokens,
                json.dumps(prompt_to_store)
                if isinstance(prompt_to_store, list)
                else prompt_to_store,
                json.dumps(response_to_store)
                if isinstance(response_to_store, dict)
                else response_to_store,
                prompt_hash,
                response_hash,
                status_code,
                error,
                user_id,
                session_id,
                json.dumps(metadata) if metadata else None,
                json.dumps(tools) if tools else None,
                cost_usd,
                upstream_cost_usd,
            )

    async def cleanup(self) -> None:
        """Close the asyncpg connection pool if it exists."""
        if self.pool:
            await self.pool.close()
            self.pool = None


__all__ = [
    "DatabaseLogger",
    "calculate_cost",
    "compute_prompt_hash",
    "compute_prompt_hash_chunked",
]
