"""Tests for D1LogStore — buffered writes and query methods.

Uses the same SQLite shim as test_d1_sqlite_integration.py to run
D1LogStore against real SQLite, catching SQL syntax errors that mocks miss.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pytest

from serving.storage.d1_log import D1LogStore, _derive_outcome

# ---------------------------------------------------------------------------
# SQLite shim (reuses the same pattern as d1_operational tests)
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    changes: int = 0
    last_row_id: int = 0
    duration_ms: float = 0.0
    rows_read: int = 0
    rows_written: int = 0


class _SqliteD1Client:
    """Thin D1Client-compatible wrapper around an in-memory SQLite database."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    async def query(self, sql: str, params: list[Any] | None = None) -> _FakeResult:
        """Execute a single statement and return rows."""
        cursor = self._conn.execute(sql, params or [])
        cols = [d[0] for d in cursor.description] if cursor.description else []
        raw = cursor.fetchall()
        rows = [dict(zip(cols, r, strict=False)) for r in raw]
        return _FakeResult(
            rows=rows,
            changes=self._conn.total_changes,
            last_row_id=cursor.lastrowid or 0,
        )

    async def execute(self, sql: str, params: list[Any] | None = None) -> _FakeResult:
        """Execute a write statement."""
        return await self.query(sql, params)

    async def batch(self, statements: list[tuple[str, list[Any] | None]]) -> list[_FakeResult]:
        """Execute statements atomically."""
        results = []
        try:
            for sql, params in statements:
                results.append(await self.query(sql, params))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return results

    async def health_check(self) -> bool:
        """Always healthy."""
        return True

    async def close(self) -> None:
        """Close SQLite connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_client():
    """Create a fresh SQLite client for each test."""
    return _SqliteD1Client()


@pytest.fixture
async def store(sqlite_client):
    """Create an initialized D1LogStore with small flush thresholds for testing."""
    s = D1LogStore(
        sqlite_client,
        flush_interval=60.0,  # long interval — we flush manually in tests
        flush_size=100,  # large size — we flush manually in tests
    )
    await s.initialize()
    # Cancel the periodic flush so it doesn't interfere with tests
    if s._flush_task:
        s._flush_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await s._flush_task
    yield s
    s._running = False


# ---------------------------------------------------------------------------
# Helper to insert logs directly (bypasses buffer)
# ---------------------------------------------------------------------------


async def _insert_log(store: D1LogStore, **overrides: Any) -> None:
    """Log a request and flush immediately."""
    defaults = {
        "request_id": f"req-{id(overrides)}",
        "model_id": "gpt-4",
        "provider": "openai",
        "prompt": "hello",
        "response": None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "latency_ms": 100,
        "status_code": 200,
        "metadata": {"user_id": "user1"},
        "pricing": {"input": "0.01", "output": "0.03"},
    }
    defaults.update(overrides)
    await store.log_request(**defaults)
    await store.flush()


# ---------------------------------------------------------------------------
# Tests: outcome derivation
# ---------------------------------------------------------------------------


class TestOutcome:
    """Test _derive_outcome helper."""

    def test_success(self):
        assert _derive_outcome(200, None) == "success"

    def test_success_201(self):
        assert _derive_outcome(201, None) == "success"

    def test_rate_limited(self):
        assert _derive_outcome(429, None) == "rate_limited"

    def test_timeout(self):
        assert _derive_outcome(500, "Connection timeout after 30s") == "timeout"

    def test_error(self):
        assert _derive_outcome(500, "Internal server error") == "error"

    def test_error_no_message(self):
        assert _derive_outcome(502, None) == "error"


# ---------------------------------------------------------------------------
# Tests: buffer and flush
# ---------------------------------------------------------------------------


class TestBufferFlush:
    """Test buffered write behavior."""

    async def test_log_request_buffers(self, store):
        """log_request should add to buffer, not write immediately."""
        await store.log_request(
            request_id="req-1",
            model_id="gpt-4",
            provider="openai",
            prompt="hi",
            response=None,
            usage=None,
            latency_ms=50,
            status_code=200,
        )
        # Not flushed yet
        result = await store._d1.query("SELECT COUNT(*) AS cnt FROM api_logs")
        assert result.rows[0]["cnt"] == 0

        # Flush
        count = await store.flush()
        assert count == 1

        result = await store._d1.query("SELECT COUNT(*) AS cnt FROM api_logs")
        assert result.rows[0]["cnt"] == 1

    async def test_flush_empty_buffer(self, store):
        """Flushing an empty buffer returns 0."""
        count = await store.flush()
        assert count == 0

    async def test_flush_size_trigger(self, sqlite_client):
        """Buffer auto-flushes when reaching flush_size."""
        s = D1LogStore(sqlite_client, flush_interval=60.0, flush_size=3)
        await s.initialize()
        if s._flush_task:
            s._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await s._flush_task

        # Add 2 rows — should not auto-flush
        for i in range(2):
            await s.log_request(
                request_id=f"req-{i}",
                model_id="gpt-4",
                provider="openai",
                prompt="hi",
                response=None,
                usage=None,
                latency_ms=50,
                status_code=200,
            )

        result = await sqlite_client.query("SELECT COUNT(*) AS cnt FROM api_logs")
        assert result.rows[0]["cnt"] == 0

        # 3rd row triggers auto-flush
        await s.log_request(
            request_id="req-2",
            model_id="gpt-4",
            provider="openai",
            prompt="hi",
            response=None,
            usage=None,
            latency_ms=50,
            status_code=200,
        )

        result = await sqlite_client.query("SELECT COUNT(*) AS cnt FROM api_logs")
        assert result.rows[0]["cnt"] == 3

        s._running = False

    async def test_duplicate_request_id_ignored(self, store):
        """ON CONFLICT(request_id) DO NOTHING — duplicates are silently ignored."""
        await _insert_log(store, request_id="dup-1")
        await _insert_log(store, request_id="dup-1")

        result = await store._d1.query("SELECT COUNT(*) AS cnt FROM api_logs")
        assert result.rows[0]["cnt"] == 1

    async def test_cleanup_flushes(self, sqlite_client):
        """cleanup() should flush remaining buffer."""
        s = D1LogStore(sqlite_client, flush_interval=60.0, flush_size=100)
        await s.initialize()
        if s._flush_task:
            s._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await s._flush_task

        await s.log_request(
            request_id="req-cleanup",
            model_id="gpt-4",
            provider="openai",
            prompt="hi",
            response=None,
            usage=None,
            latency_ms=50,
            status_code=200,
        )

        # Not flushed yet
        result = await sqlite_client.query("SELECT COUNT(*) AS cnt FROM api_logs")
        assert result.rows[0]["cnt"] == 0

        await s.cleanup()

        result = await sqlite_client.query("SELECT COUNT(*) AS cnt FROM api_logs")
        assert result.rows[0]["cnt"] == 1


# ---------------------------------------------------------------------------
# Tests: slim row content
# ---------------------------------------------------------------------------


class TestSlimRows:
    """Test that slim rows contain the expected fields."""

    async def test_row_fields(self, store):
        await _insert_log(
            store,
            request_id="req-fields",
            model_id="claude-3",
            provider="anthropic",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            latency_ms=250,
            status_code=200,
            ttft_ms=45,
            metadata={"user_id": "alice"},
        )

        result = await store._d1.query(
            "SELECT * FROM api_logs WHERE request_id = ?", ["req-fields"]
        )
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row["model_id"] == "claude-3"
        assert row["provider"] == "anthropic"
        assert row["user_id"] == "alice"
        assert row["latency_ms"] == 250
        assert row["status_code"] == 200
        assert row["ttft_ms"] == 45
        assert row["prompt_tokens"] == 100
        assert row["completion_tokens"] == 50
        assert row["outcome"] == "success"
        assert row["timestamp"] is not None

    async def test_error_outcome_stored(self, store):
        await _insert_log(
            store,
            request_id="req-err",
            status_code=500,
            error="Internal error",
        )

        result = await store._d1.query(
            "SELECT outcome FROM api_logs WHERE request_id = ?", ["req-err"]
        )
        assert result.rows[0]["outcome"] == "error"

    async def test_rate_limited_outcome(self, store):
        await _insert_log(
            store,
            request_id="req-429",
            status_code=429,
        )

        result = await store._d1.query(
            "SELECT outcome FROM api_logs WHERE request_id = ?", ["req-429"]
        )
        assert result.rows[0]["outcome"] == "rate_limited"


# ---------------------------------------------------------------------------
# Tests: cost queries
# ---------------------------------------------------------------------------


class TestCostQueries:
    """Test cost/usage query methods against real SQLite."""

    async def test_get_user_cost_today(self, store):
        """get_user_cost_today returns sum for today's rows."""
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # Insert directly (bypassing buffer for controlled timestamp)
        await store._d1.execute(
            "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
            "cost_usd, latency_ms, status_code, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["r1", today, "alice", "gpt-4", "openai", 1.50, 100, 200, "success"],
        )
        await store._d1.execute(
            "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
            "cost_usd, latency_ms, status_code, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["r2", today, "alice", "gpt-4", "openai", 2.50, 100, 200, "success"],
        )

        cost = await store.get_user_cost_today("alice")
        assert cost == pytest.approx(4.0)

    async def test_get_user_cost_today_no_rows(self, store):
        cost = await store.get_user_cost_today("nobody")
        assert cost == 0.0

    async def test_get_user_cost_period_month(self, store):
        """Month period sums all rows in current month."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        ts1 = now.strftime("%Y-%m-01T10:00:00.000000Z")
        ts2 = now.strftime("%Y-%m-15T10:00:00.000000Z")

        await store._d1.execute(
            "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
            "cost_usd, latency_ms, status_code, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["r1", ts1, "bob", "gpt-4", "openai", 5.0, 100, 200, "success"],
        )
        await store._d1.execute(
            "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
            "cost_usd, latency_ms, status_code, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["r2", ts2, "bob", "gpt-4", "openai", 10.0, 100, 200, "success"],
        )

        cost = await store.get_user_cost_period("bob", "month")
        assert cost == pytest.approx(15.0)

    async def test_get_batch_usage_empty(self, store):
        result = await store.get_batch_usage([], period="today")
        assert result == {}

    async def test_get_batch_usage(self, store):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        for uid, cost in [("alice", 3.0), ("alice", 2.0), ("bob", 7.0)]:
            rid = f"r-{uid}-{cost}"
            await store._d1.execute(
                "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
                "cost_usd, latency_ms, status_code, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [rid, today, uid, "gpt-4", "openai", cost, 100, 200, "success"],
            )

        result = await store.get_batch_usage(["alice", "bob"], period="today")
        assert result["alice"] == pytest.approx(5.0)
        assert result["bob"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Tests: detail usage queries
# ---------------------------------------------------------------------------


class TestDetailUsage:
    """Test admin detail usage methods."""

    async def test_get_user_usage_detail(self, store):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        await store._d1.execute(
            "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
            "cost_usd, latency_ms, status_code, prompt_tokens, completion_tokens, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["r1", today, "carol", "gpt-4", "openai", 2.0, 100, 200, 123, 45, "success"],
        )
        await store._d1.execute(
            "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
            "cost_usd, latency_ms, status_code, prompt_tokens, completion_tokens, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["r2", today, "carol", "gpt-4", "openai", 1.0, 80, 200, 77, 22, "success"],
        )

        detail = await store.get_user_usage_detail("carol")
        assert detail["today"]["cost_usd"] == pytest.approx(3.0)
        assert detail["today"]["requests"] == 2
        assert detail["today"]["prompt_tokens"] == 200
        assert detail["today"]["completion_tokens"] == 67
        assert detail["alltime"]["cost_usd"] == pytest.approx(3.0)
        assert detail["alltime"]["prompt_tokens"] == 200
        assert detail["alltime"]["completion_tokens"] == 67

    async def test_get_key_detail_usage(self, store):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        await store._d1.execute(
            "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
            "cost_usd, latency_ms, status_code, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["r1", today, "dave", "claude-3", "anthropic", 1.5, 200, 200, "success"],
        )

        detail = await store.get_key_detail_usage("dave")
        assert detail["today"]["cost_usd"] == pytest.approx(1.5)
        assert detail["today"]["requests"] == 1
        assert "models_used" in detail
        assert "claude-3" in detail["models_used"]

    async def test_get_user_detail_usage(self, store):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        await store._d1.execute(
            "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
            "cost_usd, latency_ms, status_code, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["r1", today, "eve", "gpt-4", "openai", 3.0, 150, 200, "success"],
        )

        detail = await store.get_user_detail_usage("eve")
        assert detail["usage_today_usd"] == pytest.approx(3.0)
        assert detail["usage_today_requests"] == 1
        assert "gpt-4" in detail["models_used"]


# ---------------------------------------------------------------------------
# Tests: analytics
# ---------------------------------------------------------------------------


class TestAnalytics:
    """Test model activity and stats queries."""

    async def test_get_model_activity(self, store):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        for i in range(3):
            await store._d1.execute(
                "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
                "cost_usd, latency_ms, status_code, ttft_ms, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [f"r{i}", now, "user1", "gpt-4", "openai", 0.1, 100 + i * 10, 200, 50, "success"],
            )

        activity = await store.get_model_activity(window_minutes=10)
        assert "gpt-4::openai" in activity
        entry = activity["gpt-4::openai"]
        assert entry["request_count"] == 3
        assert entry["success_count"] == 3

    async def test_get_stats(self, store):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        await store._d1.execute(
            "INSERT INTO api_logs (request_id, timestamp, user_id, model_id, provider, "
            "cost_usd, latency_ms, status_code, prompt_tokens, completion_tokens, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["r1", now, "user1", "gpt-4", "openai", 0.5, 100, 200, 50, 25, "success"],
        )

        stats = await store.get_stats(hours=24)
        assert len(stats) >= 1
        assert stats[0]["model_id"] == "gpt-4"
        assert stats[0]["request_count"] == 1

    async def test_get_model_activity_empty(self, store):
        activity = await store.get_model_activity(window_minutes=10)
        assert activity == {}
