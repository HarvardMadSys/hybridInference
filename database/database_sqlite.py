"""SQLite database logger for easy demo without PostgreSQL setup."""

import json
import sqlite3
import time
from typing import Dict, Any, Optional, List
from datetime import datetime
import asyncio
from contextlib import contextmanager


class SQLiteDatabaseLogger:
    """Simple SQLite logger for demo purposes."""
    
    def __init__(self, db_path: str = "openrouter_logs.db"):
        self.db_path = db_path
        self.initialized = False
    
    async def initialize(self):
        """Initialize database tables."""
        await asyncio.get_event_loop().run_in_executor(None, self._init_sync)
        self.initialized = True
    
    def _init_sync(self):
        """Synchronous initialization."""
        with self._get_connection() as conn:
            # Create main logs table
            conn.execute('''
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
            ''')
            
            # Create indexes
            conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON api_logs(timestamp DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_model ON api_logs(model_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_provider ON api_logs(provider)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_request_id ON api_logs(request_id)')
            
            # Create summary view
            conn.execute('''
                CREATE VIEW IF NOT EXISTS api_logs_summary AS
                SELECT 
                    DATE(timestamp) as date,
                    model_id,
                    provider,
                    COUNT(*) as request_count,
                    SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count,
                    SUM(prompt_tokens) as total_prompt_tokens,
                    SUM(completion_tokens) as total_completion_tokens,
                    SUM(total_tokens) as total_tokens,
                    AVG(latency_ms) as avg_latency_ms,
                    MIN(latency_ms) as min_latency_ms,
                    MAX(latency_ms) as max_latency_ms
                FROM api_logs
                GROUP BY DATE(timestamp), model_id, provider
            ''')
            
            conn.commit()
    
    @contextmanager
    def _get_connection(self):
        """Get database connection context manager."""
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
        prompt: List[Dict[str, Any]],
        response: Optional[Dict[str, Any]],
        usage: Optional[Dict[str, int]],
        latency_ms: int,
        status_code: int,
        error: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Log a request asynchronously."""
        await asyncio.get_event_loop().run_in_executor(
            None,
            self._log_request_sync,
            request_id, model_id, provider, prompt, response,
            usage, latency_ms, status_code, error, params, metadata
        )
    
    def _log_request_sync(
        self,
        request_id: str,
        model_id: str,
        provider: str,
        prompt: List[Dict[str, Any]],
        response: Optional[Dict[str, Any]],
        usage: Optional[Dict[str, int]],
        latency_ms: int,
        status_code: int,
        error: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Synchronous request logging."""
        with self._get_connection() as conn:
            try:
                conn.execute('''
                    INSERT OR IGNORE INTO api_logs (
                        request_id, model_id, provider,
                        prompt, response,
                        prompt_tokens, completion_tokens, total_tokens,
                        latency_ms, status_code, error,
                        temperature, top_p, max_tokens, seed,
                        user_id, session_id, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
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
                    metadata.get("user_id") if metadata else None,
                    metadata.get("session_id") if metadata else None,
                    json.dumps(metadata) if metadata else None
                ))
                conn.commit()
                print(f"📊 Logged request {request_id} to database")
            except Exception as e:
                print(f"Failed to log request: {e}")
    
    async def get_stats(
        self,
        model_id: Optional[str] = None,
        provider: Optional[str] = None,
        hours: int = 24
    ) -> List[Dict[str, Any]]:
        """Get statistics from the database."""
        return await asyncio.get_event_loop().run_in_executor(
            None,
            self._get_stats_sync,
            model_id, provider, hours
        )
    
    def _get_stats_sync(
        self,
        model_id: Optional[str] = None,
        provider: Optional[str] = None,
        hours: int = 24
    ) -> List[Dict[str, Any]]:
        """Synchronous stats retrieval."""
        with self._get_connection() as conn:
            query = '''
                SELECT * FROM api_logs_summary
                WHERE datetime(date) >= datetime('now', ? || ' hours')
            '''
            params = [-hours]
            
            if model_id:
                query += ' AND model_id = ?'
                params.append(model_id)
            
            if provider:
                query += ' AND provider = ?'
                params.append(provider)
            
            query += ' ORDER BY date DESC'
            
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    async def get_recent_logs(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent log entries for demo."""
        return await asyncio.get_event_loop().run_in_executor(
            None,
            self._get_recent_logs_sync,
            limit
        )
    
    def _get_recent_logs_sync(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent logs synchronously."""
        with self._get_connection() as conn:
            cursor = conn.execute('''
                SELECT 
                    request_id,
                    timestamp,
                    model_id,
                    provider,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    latency_ms,
                    status_code,
                    temperature,
                    top_p,
                    max_tokens
                FROM api_logs
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (limit,))
            
            logs = []
            for row in cursor.fetchall():
                log = dict(row)
                # Format timestamp for readability
                if log['timestamp']:
                    log['timestamp'] = str(log['timestamp'])
                logs.append(log)
            
            return logs
    
    async def get_detailed_log(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed log entry including prompt and response."""
        return await asyncio.get_event_loop().run_in_executor(
            None,
            self._get_detailed_log_sync,
            request_id
        )
    
    def _get_detailed_log_sync(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed log synchronously."""
        with self._get_connection() as conn:
            cursor = conn.execute('''
                SELECT * FROM api_logs WHERE request_id = ?
            ''', (request_id,))
            
            row = cursor.fetchone()
            if row:
                log = dict(row)
                # Parse JSON fields
                if log['prompt']:
                    log['prompt'] = json.loads(log['prompt'])
                if log['response']:
                    log['response'] = json.loads(log['response'])
                if log['metadata']:
                    log['metadata'] = json.loads(log['metadata'])
                return log
            
            return None
    
    async def cleanup(self):
        """Cleanup (nothing to do for SQLite)."""
        pass