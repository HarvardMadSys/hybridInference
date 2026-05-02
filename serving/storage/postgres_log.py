"""PostgreSQL implementation of LogStore (api_logs + api_stats_hourly)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from serving.storage.base import LogStore, Row
from serving.storage.utils import (
    calculate_cost,
    compute_prompt_hash,
    compute_prompt_hash_chunked,
)
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)


class PostgresLogStore(LogStore):
    """LogStore backed by an asyncpg connection pool."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        store_full_prompts: bool = True,
        use_chunked_hash: bool = False,
    ) -> None:
        """Initialize with an existing asyncpg pool.

        Args:
            pool: Shared asyncpg connection pool.
            store_full_prompts: If False, only store prompt_hash (privacy mode).
            use_chunked_hash: Use 4-token chunked hashing instead of full hash.
        """
        self.pool = pool
        self.store_full_prompts = store_full_prompts
        self.use_chunked_hash = use_chunked_hash

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Create api_logs and api_stats_hourly tables with indexes."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_logs (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    request_id TEXT NOT NULL UNIQUE,
                    model_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    temperature FLOAT,
                    top_p FLOAT,
                    max_tokens INTEGER,
                    seed INTEGER,
                    stream BOOLEAN,
                    ttft_ms INTEGER,
                    latency_ms INTEGER,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    total_tokens INTEGER,
                    prompt TEXT,
                    response TEXT,
                    prompt_hash TEXT,
                    response_hash TEXT,
                    status_code INTEGER,
                    error TEXT,
                    user_id TEXT,
                    session_id TEXT,
                    metadata JSONB,
                    tools JSONB,
                    cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    cost_usd DECIMAL(12, 8),
                    upstream_cost_usd DECIMAL(12, 8)
                )
            """)

            # Indexes
            for ddl in [
                "CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp DESC)",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_model ON api_logs(model_id, timestamp DESC)",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_provider ON api_logs(provider, timestamp DESC)",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_request_id ON api_logs(request_id)",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_user ON api_logs(user_id, timestamp DESC) WHERE user_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_session ON api_logs(session_id, timestamp DESC) WHERE session_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_model_activity ON api_logs(timestamp DESC, model_id, provider) WHERE user_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_error ON api_logs(timestamp DESC) WHERE error IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_prompt_hash ON api_logs(prompt_hash) WHERE prompt_hash IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_response_hash ON api_logs(response_hash) WHERE response_hash IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_api_logs_user_cost ON api_logs(user_id, timestamp, cost_usd)",
            ]:
                await conn.execute(ddl)

            # Migrations for existing databases
            for col_ddl in [
                "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER",
                "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS stream BOOLEAN",
                "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS ttft_ms INTEGER",
                "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS prompt_hash TEXT",
                "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS response_hash TEXT",
                "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER",
                "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER",
                "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS cost_usd DECIMAL(12, 8)",
                "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS upstream_cost_usd DECIMAL(12, 8)",
            ]:
                await conn.execute(col_ddl)

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

    async def cleanup(self) -> None:
        """No-op — pool lifecycle is managed externally."""

    async def health_check(self) -> bool:
        """Run ``SELECT 1`` to verify connectivity."""
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    # -- request logging -----------------------------------------------------

    async def log_request(
        self,
        *,
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

        upstream_cost_usd: OpenRouter-reported per-request upstream cost (USD),
        or None for non-OpenRouter routes.
        """
        # Auto-compute hashes
        hash_fn = compute_prompt_hash_chunked if self.use_chunked_hash else compute_prompt_hash
        if prompt_hash is None:
            prompt_hash = hash_fn(prompt)
        if response_hash is None and response is not None:
            resp_str = (
                json.dumps(response, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                if isinstance(response, dict)
                else str(response)
            )
            response_hash = hash_fn(resp_str)

        should_store_full = (
            store_full_content if store_full_content is not None else self.store_full_prompts
        )
        if should_store_full:
            prompt_str = json.dumps(prompt) if isinstance(prompt, list) else str(prompt)
            response_str = (
                json.dumps(response)
                if isinstance(response, dict)
                else str(response)
                if response is not None
                else None
            )
        else:
            prompt_str = None
            response_str = None

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
                    prompt, response, prompt_hash, response_hash,
                    status_code, error, user_id, session_id, metadata,
                    tools, upstream_cost_usd
                )
                VALUES (
                    $1, $2, $3,
                    $4, $5, $6, $7, $8,
                    $9, $10,
                    $11, $12, $13, $14,
                    $15, $16, $17,
                    $18, $19, $20, $21,
                    $22, $23, $24, $25, $26::jsonb,
                    $27::jsonb, $28
                )
                ON CONFLICT (request_id) DO NOTHING
                """,
                request_id,
                model_id,
                provider,
                (params or {}).get("temperature"),
                (params or {}).get("top_p"),
                (params or {}).get("max_tokens"),
                (params or {}).get("seed"),
                (params or {}).get("stream"),
                ttft_ms,
                latency_ms,
                (usage or {}).get("prompt_tokens"),
                (usage or {}).get("completion_tokens"),
                (usage or {}).get("reasoning_tokens"),
                (usage or {}).get("total_tokens"),
                (usage or {}).get("cache_read_tokens"),
                (usage or {}).get("cache_write_tokens"),
                cost_usd,
                prompt_str,
                response_str,
                prompt_hash,
                response_hash,
                status_code,
                error,
                (metadata or {}).get("user_id"),
                (metadata or {}).get("session_id"),
                json.dumps(metadata) if metadata else None,
                json.dumps((params or {}).get("tools")) if (params or {}).get("tools") else None,
                upstream_cost_usd,
            )

    # -- usage / cost queries ------------------------------------------------

    async def get_user_cost_today(self, user_id: str) -> float:
        """Return total cost_usd since UTC midnight."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost_spent
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
        return float(row["cost_spent"]) if row else 0.0

    async def get_user_cost_period(
        self,
        user_id: str,
        period: Literal["today", "month"],
    ) -> float:
        """Return total cost_usd within the given period."""
        trunc = "day" if period == "today" else "month"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT COALESCE(SUM(cost_usd), 0) AS cost_spent
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('{trunc}', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
        return float(row["cost_spent"]) if row else 0.0

    async def get_user_usage_detail(self, user_id: str) -> dict[str, Any]:
        """Return detailed usage stats for the user dashboard."""
        async with self.pool.acquire() as conn:
            today = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            week = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= NOW() - INTERVAL '7 days'
                """,
                user_id,
            )
            month = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            alltime = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                """,
                user_id,
            )

        def _extract(row: Any) -> dict[str, Any]:
            if not row:
                return {"cost_usd": 0.0, "requests": 0}
            return {"cost_usd": float(row["cost"]), "requests": int(row["reqs"])}

        return {
            "today": _extract(today),
            "week": _extract(week),
            "month": _extract(month),
            "alltime": _extract(alltime),
        }

    async def get_batch_usage(
        self,
        user_ids: list[str],
        period: Literal["today", "month"],
    ) -> dict[str, float]:
        """Return ``{user_id: cost_usd}`` for a batch of users."""
        if not user_ids:
            return {}
        trunc = "day" if period == "today" else "month"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost
                FROM api_logs
                WHERE user_id = ANY($1::text[])
                  AND timestamp >= date_trunc('{trunc}', NOW() AT TIME ZONE 'UTC')
                GROUP BY user_id
                """,
                user_ids,
            )
        return {row["user_id"]: float(row["cost"]) for row in rows}

    async def get_user_detail_usage(self, user_id: str) -> dict[str, Any]:
        """Return usage detail for admin user-detail view."""
        async with self.pool.acquire() as conn:
            today = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            month = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            models_rows = await conn.fetch(
                """
                SELECT DISTINCT model_id FROM api_logs
                WHERE user_id = $1 AND timestamp >= NOW() - INTERVAL '30 days'
                ORDER BY model_id
                """,
                user_id,
            )
            last_req = await conn.fetchrow(
                "SELECT MAX(timestamp) AS ts FROM api_logs WHERE user_id = $1",
                user_id,
            )

        return {
            "usage_today_usd": float(today["cost"]) if today else 0.0,
            "usage_today_requests": int(today["reqs"]) if today else 0,
            "usage_month_usd": float(month["cost"]) if month else 0.0,
            "usage_month_requests": int(month["reqs"]) if month else 0,
            "models_used": [r["model_id"] for r in models_rows],
            "last_request_at": last_req["ts"] if last_req and last_req["ts"] else None,
        }

    async def get_key_detail_usage(self, user_id: str) -> dict[str, Any]:
        """Return usage detail for admin key-detail view."""
        async with self.pool.acquire() as conn:
            today = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            month = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs
                FROM api_logs
                WHERE user_id = $1
                  AND timestamp >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
                """,
                user_id,
            )
            models_rows = await conn.fetch(
                """
                SELECT DISTINCT model_id FROM api_logs
                WHERE user_id = $1 AND timestamp >= NOW() - INTERVAL '30 days'
                ORDER BY model_id
                """,
                user_id,
            )
            last_req = await conn.fetchrow(
                "SELECT MAX(timestamp) AS ts FROM api_logs WHERE user_id = $1",
                user_id,
            )

        return {
            "today": {
                "cost_usd": float(today["cost"]) if today else 0.0,
                "requests": int(today["reqs"]) if today else 0,
            },
            "this_month": {
                "cost_usd": float(month["cost"]) if month else 0.0,
                "requests": int(month["reqs"]) if month else 0,
            },
            "models_used": [r["model_id"] for r in models_rows],
            "last_request_at": last_req["ts"] if last_req and last_req["ts"] else None,
        }

    # -- analytics -----------------------------------------------------------

    async def get_model_activity(self, window_minutes: int = 10) -> dict[str, Any]:
        """Aggregate recent real-user traffic per (model_id, provider)."""
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

        def _iso(ts: object) -> str | None:
            if ts is None:
                return None
            return ts.isoformat().replace("+00:00", "Z")  # type: ignore[union-attr]

        def _round_or_none(val: object) -> float | None:
            if val is None:
                return None
            return round(float(val), 1)

        result: dict[str, Any] = {}
        for row in rows:
            key = f"{row['model_id']}::{row['provider']}"
            result[key] = {
                "request_count": row["request_count"],
                "success_count": row["success_count"],
                "last_request_at": _iso(row["last_request_at"]),
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
        self,
        *,
        model_id: str | None = None,
        provider: str | None = None,
        hours: int = 24,
    ) -> list[Row]:
        """Fetch aggregated hourly stats."""
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
                str(hours),
                model_id,
                provider,
            )
        return [dict(r) for r in rows]
