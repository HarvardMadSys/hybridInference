import json
from typing import Any

import asyncpg


class DatabaseLogger:
    def __init__(self, db_config: dict[str, str]):
        self.db_config = db_config
        self.pool: asyncpg.Pool | None = None

    async def initialize(self):
        self.pool = await asyncpg.create_pool(
            **self.db_config, min_size=2, max_size=10, command_timeout=60
        )
        await self._create_tables()

    async def _create_tables(self):
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

            # Create a view for easy querying
            await conn.execute("""
                CREATE OR REPLACE VIEW api_logs_summary AS
                SELECT
                    DATE_TRUNC('hour', timestamp) as hour,
                    model_id,
                    provider,
                    COUNT(*) as request_count,
                    SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count,
                    SUM(prompt_tokens) as total_prompt_tokens,
                    SUM(completion_tokens) as total_completion_tokens,
                    SUM(total_tokens) as total_tokens,
                    AVG(latency_ms) as avg_latency_ms,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms) as p50_latency_ms,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_latency_ms,
                    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms) as p99_latency_ms
                FROM api_logs
                GROUP BY DATE_TRUNC('hour', timestamp), model_id, provider
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
    ):
        if not self.pool:
            return

        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO api_logs (
                        request_id, model_id, provider,
                        prompt, response,
                        prompt_tokens, completion_tokens, total_tokens,
                        latency_ms, status_code, error,
                        temperature, top_p, max_tokens, seed,
                        tools, response_format,
                        user_id, session_id, metadata
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12, $13, $14, $15, $16, $17, $18, $19, $20
                    )
                    ON CONFLICT (request_id) DO NOTHING
                """,
                    request_id,
                    model_id,
                    provider,
                    json.dumps(prompt),
                    json.dumps(response) if response else None,
                    usage.get("prompt_tokens") if usage else None,
                    usage.get("completion_tokens") if usage else None,
                    usage.get("total_tokens") if usage else None,
                    latency_ms,
                    status_code,
                    error,
                    params.get("temperature") if params else None,
                    params.get("top_p") if params else None,
                    params.get("max_tokens") if params else None,
                    params.get("seed") if params else None,
                    json.dumps(params.get("tools")) if params and params.get("tools") else None,
                    json.dumps(params.get("response_format"))
                    if params and params.get("response_format")
                    else None,
                    metadata.get("user_id") if metadata else None,
                    metadata.get("session_id") if metadata else None,
                    json.dumps(metadata) if metadata else None,
                )
        except Exception as e:
            print(f"Failed to log request: {e}")

    async def get_stats(
        self, model_id: str | None = None, provider: str | None = None, hours: int = 24
    ) -> list[dict[str, Any]]:
        if not self.pool:
            return []

        query = """
            SELECT
                hour,
                model_id,
                provider,
                request_count,
                success_count,
                error_count,
                total_prompt_tokens,
                total_completion_tokens,
                total_tokens,
                avg_latency_ms,
                p50_latency_ms,
                p95_latency_ms,
                p99_latency_ms
            FROM api_logs_summary
            WHERE hour >= NOW() - INTERVAL '%s hours'
        """

        conditions = []
        params = [hours]

        if model_id:
            conditions.append(f"model_id = ${len(params) + 1}")
            params.append(model_id)

        if provider:
            conditions.append(f"provider = ${len(params) + 1}")
            params.append(provider)

        if conditions:
            query += " AND " + " AND ".join(conditions)

        query += " ORDER BY hour DESC"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    async def cleanup(self):
        if self.pool:
            await self.pool.close()
