"""PostgreSQL-backed request/metrics logger using asyncpg.

This module provides a simple database logger that writes API requests,
responses, and usage metrics into PostgreSQL tables. It is intended for
production or staging environments where PostgreSQL is available.
"""

import json
from typing import Any

import asyncpg


class DatabaseLogger:
    """Asynchronous PostgreSQL logger using a pooled connection."""

    def __init__(self, db_config: dict[str, str]):
        """Initialize the logger with a DSN/config mapping.

        Args:
            db_config: Mapping with asyncpg pool connection arguments.
        """
        self.db_config = db_config
        # Use Any to avoid mypy issues when asyncpg types are unavailable.
        self.pool: Any | None = None

    async def initialize(self) -> None:
        """Create the connection pool and ensure tables exist."""
        self.pool = await asyncpg.create_pool(
            **self.db_config, min_size=2, max_size=10, command_timeout=60
        )
        await self._create_tables()

    async def _create_tables(self) -> None:
        """Create tables and indexes if they do not exist."""
        if self.pool is None:
            raise RuntimeError("DatabaseLogger not initialized")
        async with self.pool.acquire() as conn:
            # Main logs table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_logs (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    request_id TEXT NOT NULL UNIQUE,
                    model_id TEXT NOT NULL,
                    provider TEXT NOT NULL,

                    -- Request data
                    prompt JSONB NOT NULL,
                    prompt_text TEXT GENERATED ALWAYS AS (prompt::text) STORED,

                    -- Response data
                    response JSONB,
                    response_text TEXT GENERATED ALWAYS AS (response::text) STORED,

                    -- Usage metrics
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    total_tokens INTEGER,

                    -- Performance metrics
                    latency_ms INTEGER,
                    status_code INTEGER,

                    -- Error tracking
                    error TEXT,

                    -- Additional metadata
                    user_id TEXT,
                    session_id TEXT,
                    metadata JSONB,

                    -- Request parameters
                    temperature FLOAT,
                    top_p FLOAT,
                    max_tokens INTEGER,
                    seed INTEGER,
                    tools JSONB,
                    response_format JSONB
                )
            """)

            # Indexes for performance
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp
                ON api_logs(timestamp DESC)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_model
                ON api_logs(model_id, timestamp DESC)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_provider
                ON api_logs(provider, timestamp DESC)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_request_id
                ON api_logs(request_id)
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_user
                ON api_logs(user_id, timestamp DESC)
                WHERE user_id IS NOT NULL
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_session
                ON api_logs(session_id, timestamp DESC)
                WHERE session_id IS NOT NULL
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_logs_error
                ON api_logs(timestamp DESC)
                WHERE error IS NOT NULL
            """)

            # Add reasoning_tokens column if it doesn't exist (migration)
            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER
            """)

            # Aggregated stats table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_stats_hourly (
                    hour TIMESTAMPTZ NOT NULL,
                    model_id TEXT NOT NULL,
                    provider TEXT NOT NULL,

                    request_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,

                    total_prompt_tokens BIGINT DEFAULT 0,
                    total_completion_tokens BIGINT DEFAULT 0,
                    total_tokens BIGINT DEFAULT 0,

                    avg_latency_ms FLOAT,
                    p50_latency_ms INTEGER,
                    p95_latency_ms INTEGER,
                    p99_latency_ms INTEGER,
                    PRIMARY KEY (hour, model_id, provider)
                )
            """)

    async def log_request(
        self,
        request_id: str,
        model_id: str,
        provider: str,
        prompt: list[dict[str, Any]],
        response: dict[str, Any] | None,
        usage: dict[str, int] | None,
        latency_ms: int,
        status_code: int,
        error: str | None = None,
        params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert a single request log row.

        Args:
            request_id: Unique request identifier.
            model_id: Logical model identifier.
            provider: Provider name for the request.
            prompt: Request messages payload.
            response: Provider response payload.
            usage: Token usage breakdown.
            latency_ms: End-to-end latency in milliseconds.
            status_code: HTTP status code returned to client.
            error: Optional error message.
            params: Request parameters (temperature, max_tokens, etc.).
            metadata: Additional metadata (user_id, session_id, etc.).
        """
        if not self.pool:
            raise RuntimeError("DatabaseLogger not initialized")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO api_logs (
                    request_id, model_id, provider, prompt, response,
                    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens,
                    latency_ms, status_code, error, user_id, session_id, metadata,
                    temperature, top_p, max_tokens, seed, tools, response_format
                )
                VALUES ($1,$2,$3, $4::jsonb, $5::jsonb, $6,$7,$8,$9, $10,$11,$12, $13,$14,$15::jsonb,
                        $16,$17,$18,$19, $20::jsonb, $21::jsonb)
                ON CONFLICT (request_id) DO NOTHING
                """,
                request_id,
                model_id,
                provider,
                json.dumps(prompt),
                json.dumps(response) if response else None,
                usage.get("prompt_tokens") if usage else None,
                usage.get("completion_tokens") if usage else None,
                usage.get("reasoning_tokens") if usage else None,
                usage.get("total_tokens") if usage else None,
                latency_ms,
                status_code,
                error,
                (metadata or {}).get("user_id"),
                (metadata or {}).get("session_id"),
                json.dumps(metadata) if metadata else None,
                (params or {}).get("temperature"),
                (params or {}).get("top_p"),
                (params or {}).get("max_tokens"),
                (params or {}).get("seed"),
                json.dumps((params or {}).get("tools")) if (params or {}).get("tools") else None,
                json.dumps((params or {}).get("response_format"))
                if (params or {}).get("response_format")
                else None,
            )

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
