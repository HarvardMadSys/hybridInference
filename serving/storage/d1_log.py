"""Cloudflare D1 implementation of LogStore with buffered writes.

Stores slim log rows in D1 (no full prompt/response content). Writes are
buffered in memory and flushed periodically or when the buffer reaches a
size threshold. This trades durability for throughput — a hard crash loses
unflushed rows, which is an accepted design trade-off documented in the
architecture notes.

Full request/response content is handled separately by R2 archival
(not this module).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from serving.storage.base import LogStore, Row
from serving.storage.utils import calculate_cost
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from .d1_client import D1Client

logger = get_logger(__name__)

# Buffer configuration defaults
DEFAULT_FLUSH_INTERVAL_SECONDS = 5.0
MAX_BUFFER_SIZE = 5000
DEFAULT_FLUSH_SIZE = 50

# D1 api_logs DDL (slim rows — no prompt/response content)
_API_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS api_logs (
    request_id        TEXT PRIMARY KEY,
    timestamp         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    user_id           TEXT,
    model_id          TEXT NOT NULL,
    provider          TEXT NOT NULL,
    cost_usd          REAL,
    latency_ms        INTEGER,
    status_code       INTEGER,
    ttft_ms           INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    outcome           TEXT NOT NULL DEFAULT 'success'
)
"""

_API_LOGS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_user ON api_logs(user_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_api_logs_model ON api_logs(model_id, provider, timestamp DESC)",
]


def _derive_outcome(status_code: int, error: str | None) -> str:
    """Derive the outcome category from status code and error message."""
    if status_code is not None and 200 <= status_code < 400:
        return "success"
    if status_code == 429:
        return "rate_limited"
    if error and "timeout" in error.lower():
        return "timeout"
    return "error"


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class D1LogStore(LogStore):
    """LogStore backed by Cloudflare D1 with in-memory write buffer.

    Slim rows only — no prompt/response content is stored. Cost, latency,
    token counts, and outcome are captured for analytics and quota queries.

    Writes are buffered and flushed:
    - Every ``flush_interval`` seconds (default 5s)
    - When the buffer reaches ``flush_size`` rows (default 50)
    - On ``cleanup()`` (graceful shutdown)
    - On explicit ``flush()`` call (admin/test use)
    """

    def __init__(
        self,
        d1_client: D1Client,
        *,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        flush_size: int = DEFAULT_FLUSH_SIZE,
    ) -> None:
        self._d1 = d1_client
        self._flush_interval = flush_interval
        self._flush_size = flush_size
        self._buffer: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._running = False

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Create api_logs table and start the periodic flush task."""
        await self._d1.execute(_API_LOGS_DDL)
        for idx_ddl in _API_LOGS_INDEXES:
            await self._d1.execute(idx_ddl)
        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info(
            "D1LogStore initialized (flush_interval=%.1fs, flush_size=%d)",
            self._flush_interval,
            self._flush_size,
        )

    async def cleanup(self) -> None:
        """Stop the flush task and drain remaining buffer."""
        self._running = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        # Final flush
        await self.flush()

    async def health_check(self) -> bool:
        """Check D1 connectivity with a trivial query."""
        return await self._d1.health_check()

    # -- buffer overflow protection -------------------------------------------

    def _drop_overflow(self, incoming: int = 0) -> int:
        """Drop oldest buffer entries if over MAX_BUFFER_SIZE.

        Must be called while holding ``self._lock``.  Returns the number
        of entries dropped.  Emits a single rate-limited warning per call
        (i.e. once per flush cycle, not per row).
        """
        total = len(self._buffer) + incoming
        if total <= MAX_BUFFER_SIZE:
            return 0
        drop_count = total - MAX_BUFFER_SIZE
        del self._buffer[:drop_count]
        logger.warning(
            "D1LogStore buffer overflow: dropped %d oldest rows (buffer capped at %d)",
            drop_count,
            MAX_BUFFER_SIZE,
        )
        return drop_count

    # -- buffered write ------------------------------------------------------

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
    ) -> None:
        """Buffer a slim log row for later flush to D1."""
        cost_usd = calculate_cost(usage, pricing)
        user_id = (metadata or {}).get("user_id")
        outcome = _derive_outcome(status_code, error)

        row = {
            "request_id": request_id,
            "timestamp": _now_iso(),
            "user_id": user_id,
            "model_id": model_id,
            "provider": provider,
            "cost_usd": float(cost_usd) if cost_usd else None,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "ttft_ms": ttft_ms,
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
            "completion_tokens": (usage or {}).get("completion_tokens"),
            "outcome": outcome,
        }

        should_flush = False
        async with self._lock:
            self._buffer.append(row)
            self._drop_overflow()
            if len(self._buffer) >= self._flush_size:
                should_flush = True

        if should_flush:
            await self.flush()

    async def flush(self) -> int:
        """Flush buffered rows to D1. Returns count of rows flushed."""
        async with self._lock:
            if not self._buffer:
                return 0
            batch = self._buffer[:]
            self._buffer.clear()

        if not batch:
            return 0

        statements: list[tuple[str, list[Any] | None]] = []
        for row in batch:
            statements.append(
                (
                    "INSERT INTO api_logs "
                    "(request_id, timestamp, user_id, model_id, provider, "
                    "cost_usd, latency_ms, status_code, ttft_ms, "
                    "prompt_tokens, completion_tokens, outcome) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(request_id) DO NOTHING",
                    [
                        row["request_id"],
                        row["timestamp"],
                        row["user_id"],
                        row["model_id"],
                        row["provider"],
                        row["cost_usd"],
                        row["latency_ms"],
                        row["status_code"],
                        row["ttft_ms"],
                        row["prompt_tokens"],
                        row["completion_tokens"],
                        row["outcome"],
                    ],
                )
            )

        try:
            await self._d1.batch(statements)
            logger.debug("Flushed %d log rows to D1", len(batch))
            return len(batch)
        except Exception:
            # Put rows back so they aren't lost, respecting the buffer cap
            async with self._lock:
                self._buffer = batch + self._buffer
                self._drop_overflow()
            logger.exception("Failed to flush %d log rows to D1, re-queued", len(batch))
            raise

    async def _periodic_flush(self) -> None:
        """Background loop: flush buffer every ``flush_interval`` seconds."""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                if self._buffer:
                    await self.flush()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Periodic flush error")

    # -- usage / cost queries ------------------------------------------------

    async def get_user_cost_today(self, user_id: str) -> float:
        """Return total cost_usd since UTC midnight today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = await self._d1.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost_spent FROM api_logs "
            "WHERE user_id = ? AND timestamp >= ?",
            [user_id, f"{today}T00:00:00.000000Z"],
        )
        return float(result.rows[0]["cost_spent"]) if result.rows else 0.0

    async def get_user_cost_period(
        self,
        user_id: str,
        period: Literal["today", "month"],
    ) -> float:
        """Return total cost_usd within the given period."""
        if period == "today":
            return await self.get_user_cost_today(user_id)
        # month: first day of current month
        month_start = datetime.now(timezone.utc).strftime("%Y-%m-01T00:00:00.000000Z")
        result = await self._d1.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost_spent FROM api_logs "
            "WHERE user_id = ? AND timestamp >= ?",
            [user_id, month_start],
        )
        return float(result.rows[0]["cost_spent"]) if result.rows else 0.0

    async def get_user_usage_detail(self, user_id: str) -> dict[str, Any]:
        """Return detailed usage stats for user dashboard."""
        now = datetime.now(timezone.utc)
        today_start = now.strftime("%Y-%m-%dT00:00:00.000000Z")
        week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        month_start = now.strftime("%Y-%m-01T00:00:00.000000Z")

        async def _sum_period(since: str) -> dict[str, Any]:
            r = await self._d1.query(
                "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs "
                "FROM api_logs WHERE user_id = ? AND timestamp >= ?",
                [user_id, since],
            )
            if r.rows:
                return {"cost_usd": float(r.rows[0]["cost"]), "requests": int(r.rows[0]["reqs"])}
            return {"cost_usd": 0.0, "requests": 0}

        # all-time (no filter)
        r_all = await self._d1.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs "
            "FROM api_logs WHERE user_id = ?",
            [user_id],
        )
        alltime = (
            {"cost_usd": float(r_all.rows[0]["cost"]), "requests": int(r_all.rows[0]["reqs"])}
            if r_all.rows
            else {"cost_usd": 0.0, "requests": 0}
        )

        today, week, month = await asyncio.gather(
            _sum_period(today_start),
            _sum_period(week_ago),
            _sum_period(month_start),
        )

        return {
            "today": today,
            "week": week,
            "month": month,
            "alltime": alltime,
        }

    async def get_batch_usage(
        self,
        user_ids: list[str],
        period: Literal["today", "month"],
    ) -> dict[str, float]:
        """Return {user_id: cost_usd} for a batch of users."""
        if not user_ids:
            return {}
        if period == "today":
            since = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000000Z")
        else:
            since = datetime.now(timezone.utc).strftime("%Y-%m-01T00:00:00.000000Z")

        placeholders = ",".join(["?"] * len(user_ids))
        params: list[Any] = [since, *user_ids]
        result = await self._d1.query(
            f"SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost "
            f"FROM api_logs WHERE timestamp >= ? AND user_id IN ({placeholders}) "
            f"GROUP BY user_id",
            params,
        )
        return {r["user_id"]: float(r["cost"]) for r in result.rows}

    async def get_user_detail_usage(self, user_id: str) -> dict[str, Any]:
        """Return usage detail for admin user-detail view."""
        now = datetime.now(timezone.utc)
        today_start = now.strftime("%Y-%m-%dT00:00:00.000000Z")
        month_start = now.strftime("%Y-%m-01T00:00:00.000000Z")

        today = await self._d1.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs "
            "FROM api_logs WHERE user_id = ? AND timestamp >= ?",
            [user_id, today_start],
        )
        month = await self._d1.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs "
            "FROM api_logs WHERE user_id = ? AND timestamp >= ?",
            [user_id, month_start],
        )
        thirty_days_ago = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        models = await self._d1.query(
            "SELECT DISTINCT model_id FROM api_logs "
            "WHERE user_id = ? AND timestamp >= ? ORDER BY model_id",
            [user_id, thirty_days_ago],
        )
        last_req = await self._d1.query(
            "SELECT MAX(timestamp) AS ts FROM api_logs WHERE user_id = ?",
            [user_id],
        )

        return {
            "usage_today_usd": float(today.rows[0]["cost"]) if today.rows else 0.0,
            "usage_today_requests": int(today.rows[0]["reqs"]) if today.rows else 0,
            "usage_month_usd": float(month.rows[0]["cost"]) if month.rows else 0.0,
            "usage_month_requests": int(month.rows[0]["reqs"]) if month.rows else 0,
            "models_used": [r["model_id"] for r in models.rows],
            "last_request_at": last_req.rows[0]["ts"]
            if last_req.rows and last_req.rows[0]["ts"]
            else None,
        }

    async def get_key_detail_usage(self, user_id: str) -> dict[str, Any]:
        """Return usage detail for admin key-detail view."""
        now = datetime.now(timezone.utc)
        today_start = now.strftime("%Y-%m-%dT00:00:00.000000Z")
        month_start = now.strftime("%Y-%m-01T00:00:00.000000Z")

        today = await self._d1.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs "
            "FROM api_logs WHERE user_id = ? AND timestamp >= ?",
            [user_id, today_start],
        )
        month = await self._d1.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS reqs "
            "FROM api_logs WHERE user_id = ? AND timestamp >= ?",
            [user_id, month_start],
        )
        thirty_days_ago = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        models = await self._d1.query(
            "SELECT DISTINCT model_id FROM api_logs "
            "WHERE user_id = ? AND timestamp >= ? ORDER BY model_id",
            [user_id, thirty_days_ago],
        )
        last_req = await self._d1.query(
            "SELECT MAX(timestamp) AS ts FROM api_logs WHERE user_id = ?",
            [user_id],
        )

        return {
            "today": {
                "cost_usd": float(today.rows[0]["cost"]) if today.rows else 0.0,
                "requests": int(today.rows[0]["reqs"]) if today.rows else 0,
            },
            "this_month": {
                "cost_usd": float(month.rows[0]["cost"]) if month.rows else 0.0,
                "requests": int(month.rows[0]["reqs"]) if month.rows else 0,
            },
            "models_used": [r["model_id"] for r in models.rows],
            "last_request_at": last_req.rows[0]["ts"]
            if last_req.rows and last_req.rows[0]["ts"]
            else None,
        }

    # -- analytics -----------------------------------------------------------

    async def get_model_activity(self, window_minutes: int = 10) -> dict[str, Any]:
        """Aggregate recent real-user traffic per (model_id, provider)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

        result = await self._d1.query(
            "SELECT model_id, provider, "
            "COUNT(*) AS request_count, "
            "SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) AS success_count, "
            "MAX(timestamp) AS last_request_at, "
            "AVG(CASE WHEN status_code < 400 THEN latency_ms ELSE NULL END) AS avg_latency_ms, "
            "SUM(CASE WHEN ttft_ms IS NOT NULL AND status_code < 400 THEN 1 ELSE 0 END) AS stream_count, "
            "SUM(CASE WHEN ttft_ms IS NULL AND status_code < 400 THEN 1 ELSE 0 END) AS non_stream_count, "
            "AVG(CASE WHEN ttft_ms IS NOT NULL AND status_code < 400 THEN ttft_ms ELSE NULL END) AS stream_avg_ttft_ms, "
            "AVG(CASE WHEN status_code < 400 AND completion_tokens IS NOT NULL THEN completion_tokens ELSE NULL END) AS avg_completion_tokens "
            "FROM api_logs "
            "WHERE timestamp >= ? AND user_id IS NOT NULL "
            "GROUP BY model_id, provider",
            [cutoff],
        )

        def _round_or_none(val: Any) -> float | None:
            if val is None:
                return None
            return round(float(val), 1)

        output: dict[str, Any] = {}
        for row in result.rows:
            key = f"{row['model_id']}::{row['provider']}"
            output[key] = {
                "request_count": row["request_count"],
                "success_count": row["success_count"],
                "last_request_at": row["last_request_at"],
                "avg_latency_ms": _round_or_none(row["avg_latency_ms"]),
                "stream_count": row["stream_count"],
                "non_stream_count": row["non_stream_count"],
                "stream_avg_ttft_ms": _round_or_none(row["stream_avg_ttft_ms"]),
                "avg_completion_tokens": _round_or_none(row["avg_completion_tokens"]),
            }
        return output

    async def get_stats(
        self,
        *,
        model_id: str | None = None,
        provider: str | None = None,
        hours: int = 24,
    ) -> list[Row]:
        """Aggregate hourly stats from api_logs (no separate stats table in D1)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

        conditions = ["timestamp >= ?"]
        params: list[Any] = [cutoff]

        if model_id:
            conditions.append("model_id = ?")
            params.append(model_id)
        if provider:
            conditions.append("provider = ?")
            params.append(provider)

        where = " AND ".join(conditions)

        result = await self._d1.query(
            f"SELECT "
            f"  substr(timestamp, 1, 13) || ':00:00Z' AS hour, "
            f"  model_id, provider, "
            f"  COUNT(*) AS request_count, "
            f"  SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) AS success_count, "
            f"  SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS error_count, "
            f"  COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens, "
            f"  COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens, "
            f"  COALESCE(SUM(prompt_tokens) + SUM(completion_tokens), 0) AS total_tokens, "
            f"  AVG(latency_ms) AS avg_latency_ms "
            f"FROM api_logs WHERE {where} "
            f"GROUP BY substr(timestamp, 1, 13), model_id, provider "
            f"ORDER BY hour DESC "
            f"LIMIT 1000",
            params,
        )
        return [dict(r) for r in result.rows]
