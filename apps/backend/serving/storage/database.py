"""PostgreSQL-backed request/metrics logger using asyncpg.

This module provides a simple database logger that writes API requests,
responses, and usage metrics into PostgreSQL tables. It is intended for
production or staging environments where PostgreSQL is available.

Pure utility functions (``calculate_cost``, etc.) have been moved to
``serving.storage.utils`` so they can be imported without pulling in asyncpg.
"""

import json
from typing import Any

from serving.storage.utils import calculate_cost
from serving.utils.logging import get_logger
from serving.utils.token_utils import normalize_usage

logger = get_logger(__name__)


class DatabaseLogger:
    """Asynchronous PostgreSQL logger using a pooled connection."""

    def __init__(
        self,
        db_config: dict[str, str],
        store_full_prompts: bool = True,
    ):
        """Initialize the logger with a DSN/config mapping.

        Args:
            db_config: Mapping with asyncpg pool connection arguments.
            store_full_prompts: If False, prompt and response fields will be NULL.
        """
        self.db_config = db_config
        self.store_full_prompts = store_full_prompts
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
        import asyncpg

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
        usage: dict[str, Any] | None,
        latency_ms: int,
        status_code: int,
        error: str | None = None,
        params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        ttft_ms: int | None = None,
        store_full_content: bool | None = None,
        pricing: dict[str, str] | None = None,
        upstream_cost_usd: float | None = None,
        request_payload: dict[str, Any] | None = None,
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
            store_full_content: Override instance default for storing full prompt/response.
                If None, uses self.store_full_prompts. Set False for privacy mode.
            pricing: Model pricing config for cost calculation (per 1M tokens).
            upstream_cost_usd: OpenRouter-reported per-request upstream cost (USD).
                Internal accounting only — orthogonal to user-billed `cost_usd`.
                None for non-OpenRouter routes.
            request_payload: Raw incoming request body (dict). Stored as JSONB
                in the ``request_payload`` column when full-content logging
                is enabled; nulled in privacy mode.
        """
        if not self.pool:
            raise RuntimeError("DatabaseLogger not initialized")

        usage = normalize_usage(usage) or usage

        # Privacy control: use per-request override or instance default
        should_store_full = (
            store_full_content if store_full_content is not None else self.store_full_prompts
        )

        if should_store_full:
            # Store full prompt and response text
            prompt_str = json.dumps(prompt) if isinstance(prompt, list) else str(prompt)
            response_str = (
                json.dumps(response)
                if isinstance(response, dict)
                else str(response)
                if response is not None
                else None
            )
            request_payload_str = (
                json.dumps(request_payload) if request_payload is not None else None
            )
        else:
            # Privacy mode: do not persist request/response content.
            prompt_str = None
            response_str = None
            request_payload_str = None

        # Calculate cost based on usage and pricing
        cost_usd = calculate_cost(usage, pricing)

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO api_logs (
                    request_id, model_id, provider,
                    temperature, top_p, max_tokens, seed, stream,
                    ttft_ms, latency_ms,
                    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens,
                    cache_read_tokens, cache_write_tokens, cost_usd,
                    prompt, response, request_payload,
                    status_code, error, user_id, session_id, metadata,
                    tools, upstream_cost_usd
                )
                VALUES (
                    $1, $2, $3,
                    $4, $5, $6, $7, $8,
                    $9, $10,
                    $11, $12, $13, $14,
                    $15, $16, $17,
                    $18, $19, $20::jsonb,
                    $21, $22, $23, $24, $25::jsonb,
                    $26::jsonb, $27
                )
                ON CONFLICT (request_id) DO NOTHING
                """,
                request_id,
                model_id,
                provider,
                # Request parameters
                (params or {}).get("temperature"),
                (params or {}).get("top_p"),
                (params or {}).get("max_tokens"),
                (params or {}).get("seed"),
                (params or {}).get("stream"),
                # Performance metrics
                ttft_ms,
                latency_ms,
                # Token usage
                (usage or {}).get("prompt_tokens"),
                (usage or {}).get("completion_tokens"),
                (usage or {}).get("reasoning_tokens"),
                (usage or {}).get("total_tokens"),
                # Cache and cost
                (usage or {}).get("cache_read_tokens"),
                (usage or {}).get("cache_write_tokens"),
                cost_usd,
                # Content
                prompt_str,
                response_str,
                request_payload_str,
                # Metadata
                status_code,
                error,
                (metadata or {}).get("user_id"),
                (metadata or {}).get("session_id"),
                json.dumps(metadata) if metadata else None,
                json.dumps((params or {}).get("tools")) if (params or {}).get("tools") else None,
                upstream_cost_usd,
            )

    async def get_model_activity(self, window_minutes: int = 10) -> dict[str, Any]:
        """Aggregate recent real-user traffic per (model_id, provider).

        Returns a dict keyed by ``"model_id::provider"`` with per-route stats.
        Synthetic probes (``user_id IS NULL``) are excluded.
        """
        if not self.pool:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT model_id, provider,
                       COUNT(*)                                           AS request_count,
                       COUNT(*) FILTER (WHERE status_code < 400)          AS success_count,
                       MAX(timestamp)                                     AS last_request_at,
                       AVG(latency_ms) FILTER (WHERE status_code < 400)   AS avg_latency_ms,
                       COUNT(*) FILTER (WHERE stream = TRUE)              AS stream_count,
                       COUNT(*) FILTER (WHERE stream IS NOT TRUE)         AS non_stream_count,
                       COUNT(*) FILTER (WHERE stream = TRUE AND status_code < 400)
                           AS stream_success_count,
                       COUNT(*) FILTER (WHERE stream IS NOT TRUE AND status_code < 400)
                           AS non_stream_success_count,
                       MAX(timestamp) FILTER (WHERE stream = TRUE AND status_code < 400)
                           AS stream_last_success_at,
                       MAX(timestamp) FILTER (WHERE stream IS NOT TRUE AND status_code < 400)
                           AS non_stream_last_success_at,
                       AVG(ttft_ms) FILTER (WHERE stream = TRUE AND status_code < 400 AND ttft_ms IS NOT NULL)
                           AS stream_avg_ttft_ms,
                       AVG(completion_tokens) FILTER (WHERE status_code < 400 AND completion_tokens IS NOT NULL)
                           AS avg_completion_tokens,
                       AVG(completion_tokens) FILTER (WHERE stream = TRUE AND status_code < 400 AND completion_tokens IS NOT NULL)
                           AS stream_avg_completion_tokens,
                       AVG(completion_tokens) FILTER (WHERE stream IS NOT TRUE AND status_code < 400 AND completion_tokens IS NOT NULL)
                           AS non_stream_avg_completion_tokens
                FROM api_logs
                WHERE timestamp >= NOW() - ($1 || ' minutes')::interval
                  AND user_id IS NOT NULL
                GROUP BY model_id, provider
                """,
                str(window_minutes),
            )
        result: dict[str, Any] = {}
        for row in rows:
            key = f"{row['model_id']}::{row['provider']}"
            last_req = row["last_request_at"]

            def _iso(ts: object) -> str | None:
                if ts is None:
                    return None
                return ts.isoformat().replace("+00:00", "Z")  # type: ignore[union-attr]

            def _round_or_none(val: object) -> float | None:
                if val is None:
                    return None
                return round(float(val), 1)

            result[key] = {
                "request_count": row["request_count"],
                "success_count": row["success_count"],
                "last_request_at": _iso(last_req),
                "avg_latency_ms": _round_or_none(row["avg_latency_ms"]),
                "stream_count": row["stream_count"],
                "non_stream_count": row["non_stream_count"],
                "stream_success_count": row["stream_success_count"],
                "non_stream_success_count": row["non_stream_success_count"],
                "stream_last_success_at": _iso(row["stream_last_success_at"]),
                "non_stream_last_success_at": _iso(row["non_stream_last_success_at"]),
                "stream_avg_ttft_ms": _round_or_none(row["stream_avg_ttft_ms"]),
                "avg_completion_tokens": _round_or_none(row["avg_completion_tokens"]),
                "stream_avg_completion_tokens": _round_or_none(row["stream_avg_completion_tokens"]),
                "non_stream_avg_completion_tokens": _round_or_none(
                    row["non_stream_avg_completion_tokens"]
                ),
            }
        return result

    async def get_stats(
        self, model_id: str | None = None, provider: str | None = None, hours: int = 24
    ) -> list[dict[str, Any]]:
        """Fetch aggregated hourly stats for the given time window."""
        if not self.pool:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM api_stats_hourly
                WHERE hour >= NOW() - ($1 || ' hours')::interval
                AND ($2::text IS NULL OR model_id = $2)
                AND ($3::text IS NULL OR provider = $3)
                ORDER BY hour DESC
                LIMIT 1000
                """,
                hours,
                model_id,
                provider,
            )
        return [dict(r) for r in rows]

    async def cleanup(self) -> None:
        """Close the connection pool if initialized."""
        if self.pool:
            try:
                await self.pool.close()  # type: ignore[attr-defined]
            finally:
                self.pool = None
