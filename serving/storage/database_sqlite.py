"""SQLite database logger for development without PostgreSQL setup."""

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _default_db_path() -> str:
    # Use var/db by default
    project_root = Path(__file__).resolve().parents[2]
    db_dir = Path(os.getenv("DB_DIR") or (project_root / "var" / "db"))
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "openrouter_logs.db")


class SQLiteDatabaseLogger:
    """Simple SQLite logger for demo purposes."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or _default_db_path()
        self.initialized = False

    async def initialize(self) -> None:
        """Initialize database tables."""
        await asyncio.get_event_loop().run_in_executor(None, self._init_sync)
        self.initialized = True

    def _init_sync(self) -> None:
        """Synchronous initialization."""
        with self._get_connection() as conn:
            # Create main logs table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    request_id TEXT UNIQUE,
                    model_id TEXT NOT NULL,
                    provider TEXT NOT NULL,

                    -- Request data
                    prompt TEXT NOT NULL,

                    -- Response data
                    response TEXT,

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
                    metadata TEXT,

                    -- Request parameters
                    temperature REAL,
                    top_p REAL,
                    max_tokens INTEGER,
                    seed INTEGER
                )
                """
            )

            # Indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON api_logs(timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model ON api_logs(model_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_provider ON api_logs(provider)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_id ON api_logs(request_id)")

            # Drop and recreate summary view to include reasoning_tokens
            conn.execute("DROP VIEW IF EXISTS api_logs_summary")
            conn.execute(
                """
                CREATE VIEW api_logs_summary AS
                SELECT
                    DATE(timestamp) as date,
                    model_id,
                    provider,
                    COUNT(*) as request_count,
                    SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count,
                    SUM(prompt_tokens) as total_prompt_tokens,
                    SUM(completion_tokens) as total_completion_tokens,
                    SUM(reasoning_tokens) as total_reasoning_tokens,
                    SUM(total_tokens) as total_tokens,
                    AVG(latency_ms) as avg_latency_ms,
                    MIN(latency_ms) as min_latency_ms,
                    MAX(latency_ms) as max_latency_ms
                FROM api_logs
                GROUP BY DATE(timestamp), model_id, provider
                """
            )

            # Add reasoning_tokens column if it doesn't exist (migration)
            cursor = conn.execute("PRAGMA table_info(api_logs)")
            columns = [col[1] for col in cursor.fetchall()]
            if "reasoning_tokens" not in columns:
                conn.execute("ALTER TABLE api_logs ADD COLUMN reasoning_tokens INTEGER")

            conn.commit()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

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
        """Insert a single request log row into SQLite."""
        await asyncio.get_event_loop().run_in_executor(
            None,
            self._log_request_sync,
            request_id,
            model_id,
            provider,
            prompt,
            response,
            usage,
            latency_ms,
            status_code,
            error,
            params,
            metadata,
        )

    def _log_request_sync(
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
        with self._get_connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO api_logs (
                        request_id, model_id, provider,
                        prompt, response,
                        prompt_tokens, completion_tokens, reasoning_tokens, total_tokens,
                        latency_ms, status_code, error,
                        temperature, top_p, max_tokens, seed,
                        user_id, session_id, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        model_id,
                        provider,
                        json.dumps(prompt),
                        json.dumps(response) if response else None,
                        (usage or {}).get("prompt_tokens"),
                        (usage or {}).get("completion_tokens"),
                        (usage or {}).get("reasoning_tokens"),
                        (usage or {}).get("total_tokens"),
                        latency_ms,
                        status_code,
                        error,
                        (params or {}).get("temperature"),
                        (params or {}).get("top_p"),
                        (params or {}).get("max_tokens"),
                        (params or {}).get("seed"),
                        (metadata or {}).get("user_id"),
                        (metadata or {}).get("session_id"),
                        json.dumps(metadata) if metadata else None,
                    ),
                )
                conn.commit()
                logger.info("Logged request to SQLite DB: request_id=%s", request_id)
            except Exception as exc:
                logger.exception("Failed to log request: %s", exc)

    async def get_stats(
        self, model_id: str | None = None, provider: str | None = None, hours: int = 24
    ) -> list[dict[str, Any]]:
        """Fetch summarized stats over the given time window (hours)."""
        return await asyncio.get_event_loop().run_in_executor(
            None, self._get_stats_sync, model_id, provider, hours
        )

    def _get_stats_sync(
        self, model_id: str | None = None, provider: str | None = None, hours: int = 24
    ) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            query = "SELECT * FROM api_logs_summary WHERE datetime(date) >= datetime('now', ? || ' hours')"
            params: list[object] = [-hours]
            if model_id:
                query += " AND model_id = ?"
                params.append(model_id)
            if provider:
                query += " AND provider = ?"
                params.append(provider)
            query += " ORDER BY date DESC"
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    async def get_recent_logs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return most recent N log rows with key fields."""
        return await asyncio.get_event_loop().run_in_executor(
            None, self._get_recent_logs_sync, limit
        )

    def _get_recent_logs_sync(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT request_id, timestamp, model_id, provider,
                       prompt_tokens, completion_tokens, total_tokens,
                       latency_ms, status_code, temperature, top_p, max_tokens
                FROM api_logs
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            logs: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                log = dict(row)
                if log.get("timestamp"):
                    log["timestamp"] = str(log["timestamp"])
                logs.append(log)
            return logs
