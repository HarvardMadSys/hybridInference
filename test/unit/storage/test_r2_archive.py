"""Tests for R2 log archival script.

Tests the core archival logic (fetch, compress, delete) using a
SQLite-backed D1 shim. R2 uploads are mocked.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# SQLite shim (same pattern as other D1 tests)
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

    async def close(self) -> None:
        """Close connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def d1():
    """Create a SQLite D1 client with api_logs table."""
    client = _SqliteD1Client()
    client._conn.execute("""
        CREATE TABLE IF NOT EXISTS api_logs (
            request_id        TEXT PRIMARY KEY,
            timestamp         TEXT NOT NULL,
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
    """)
    client._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp DESC)"
    )
    return client


def _insert_log(d1: _SqliteD1Client, request_id: str, timestamp: str, **kwargs: Any) -> None:
    """Insert a log row directly into SQLite."""
    defaults = {
        "user_id": "user1",
        "model_id": "gpt-4",
        "provider": "openai",
        "cost_usd": 0.01,
        "latency_ms": 100,
        "status_code": 200,
        "ttft_ms": None,
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "outcome": "success",
    }
    defaults.update(kwargs)
    d1._conn.execute(
        "INSERT INTO api_logs "
        "(request_id, timestamp, user_id, model_id, provider, cost_usd, "
        "latency_ms, status_code, ttft_ms, prompt_tokens, completion_tokens, outcome) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            request_id,
            timestamp,
            defaults["user_id"],
            defaults["model_id"],
            defaults["provider"],
            defaults["cost_usd"],
            defaults["latency_ms"],
            defaults["status_code"],
            defaults["ttft_ms"],
            defaults["prompt_tokens"],
            defaults["completion_tokens"],
            defaults["outcome"],
        ],
    )
    d1._conn.commit()


# ---------------------------------------------------------------------------
# Tests: fetch
# ---------------------------------------------------------------------------


class TestFetchDayLogs:
    """Test _fetch_day_logs."""

    async def test_fetch_returns_rows_for_day(self, d1):
        from scripts.cloudflare.r2_archive_logs import _fetch_day_logs

        _insert_log(d1, "r1", "2026-04-01T10:00:00.000000Z")
        _insert_log(d1, "r2", "2026-04-01T23:59:59.000000Z")
        _insert_log(d1, "r3", "2026-04-02T00:00:01.000000Z")  # next day

        rows = await _fetch_day_logs(d1, "2026-04-01")
        assert len(rows) == 2
        assert {r["request_id"] for r in rows} == {"r1", "r2"}

    async def test_fetch_empty_day(self, d1):
        from scripts.cloudflare.r2_archive_logs import _fetch_day_logs

        rows = await _fetch_day_logs(d1, "2026-04-01")
        assert rows == []

    async def test_fetch_paginates(self, d1):
        from scripts.cloudflare.r2_archive_logs import _fetch_day_logs

        # Insert more rows than PAGE_SIZE
        for i in range(10):
            _insert_log(d1, f"r{i}", f"2026-04-01T{i:02d}:00:00.000000Z")

        # Temporarily set PAGE_SIZE to 3 for testing
        import scripts.cloudflare.r2_archive_logs as mod

        original = mod.PAGE_SIZE
        mod.PAGE_SIZE = 3
        try:
            rows = await _fetch_day_logs(d1, "2026-04-01")
            assert len(rows) == 10
        finally:
            mod.PAGE_SIZE = original


# ---------------------------------------------------------------------------
# Tests: compress
# ---------------------------------------------------------------------------


class TestCompressRows:
    """Test _compress_rows."""

    def test_compress_produces_valid_gzip(self):
        from scripts.cloudflare.r2_archive_logs import _compress_rows

        rows = [
            {"request_id": "r1", "model_id": "gpt-4", "cost_usd": 0.01},
            {"request_id": "r2", "model_id": "claude-3", "cost_usd": 0.02},
        ]
        compressed = _compress_rows(rows)

        # Decompress and verify
        decompressed = gzip.decompress(compressed).decode("utf-8")
        lines = decompressed.strip().split("\n")
        assert len(lines) == 2

        parsed = [json.loads(line) for line in lines]
        assert parsed[0]["request_id"] == "r1"
        assert parsed[1]["request_id"] == "r2"

    def test_compress_empty_rows(self):
        from scripts.cloudflare.r2_archive_logs import _compress_rows

        compressed = _compress_rows([])
        decompressed = gzip.decompress(compressed).decode("utf-8")
        assert decompressed == ""


# ---------------------------------------------------------------------------
# Tests: delete
# ---------------------------------------------------------------------------


class TestDeleteDayLogs:
    """Test _delete_day_logs."""

    async def test_delete_removes_only_target_day(self, d1):
        from scripts.cloudflare.r2_archive_logs import _delete_day_logs

        _insert_log(d1, "r1", "2026-04-01T10:00:00.000000Z")
        _insert_log(d1, "r2", "2026-04-01T20:00:00.000000Z")
        _insert_log(d1, "r3", "2026-04-02T05:00:00.000000Z")

        deleted = await _delete_day_logs(d1, "2026-04-01")
        assert deleted == 2

        # r3 should remain
        result = d1._conn.execute("SELECT COUNT(*) FROM api_logs").fetchone()
        assert result[0] == 1

    async def test_delete_no_rows(self, d1):
        from scripts.cloudflare.r2_archive_logs import _delete_day_logs

        deleted = await _delete_day_logs(d1, "2026-04-01")
        assert deleted == 0


# ---------------------------------------------------------------------------
# Tests: prune
# ---------------------------------------------------------------------------


class TestPruneOldLogs:
    """Test _prune_old_logs."""

    async def test_prune_removes_old_rows(self, d1):
        from scripts.cloudflare.r2_archive_logs import _prune_old_logs

        # Old rows (more than 30 days ago from 2026-04-09)
        _insert_log(d1, "r-old1", "2026-03-01T10:00:00.000000Z")
        _insert_log(d1, "r-old2", "2026-03-05T10:00:00.000000Z")
        # Recent row
        _insert_log(d1, "r-new", "2026-04-08T10:00:00.000000Z")

        deleted = await _prune_old_logs(d1, retention_days=30)
        assert deleted == 2

        result = d1._conn.execute("SELECT COUNT(*) FROM api_logs").fetchone()
        assert result[0] == 1


# ---------------------------------------------------------------------------
# Tests: archive_day (end-to-end with mocked R2)
# ---------------------------------------------------------------------------


class TestArchiveDay:
    """Test archive_day end-to-end."""

    async def test_archive_day_dry_run(self, d1):
        from scripts.cloudflare.r2_archive_logs import archive_day

        _insert_log(d1, "r1", "2026-04-01T10:00:00.000000Z")
        _insert_log(d1, "r2", "2026-04-01T20:00:00.000000Z")

        summary = await archive_day(
            d1,
            "2026-04-01",
            dry_run=True,
            bucket="test-bucket",
            endpoint_url="https://test.r2.dev",
            access_key_id="key",
            secret_access_key="secret",
        )

        assert summary["row_count"] == 2
        assert summary["dry_run"] is True
        assert summary["r2_key"] == "logs/2026/04/01.json.gz"

        # Rows should NOT be deleted in dry run
        result = d1._conn.execute("SELECT COUNT(*) FROM api_logs").fetchone()
        assert result[0] == 2

    async def test_archive_day_empty(self, d1):
        from scripts.cloudflare.r2_archive_logs import archive_day

        summary = await archive_day(
            d1,
            "2026-04-01",
            dry_run=True,
            bucket="test-bucket",
            endpoint_url="https://test.r2.dev",
            access_key_id="key",
            secret_access_key="secret",
        )

        assert summary["row_count"] == 0
        assert summary["skipped"] is True

    @patch("scripts.cloudflare.r2_archive_logs._upload_to_r2", new_callable=AsyncMock)
    async def test_archive_day_uploads_and_deletes(self, mock_upload, d1):
        from scripts.cloudflare.r2_archive_logs import archive_day

        _insert_log(d1, "r1", "2026-04-01T10:00:00.000000Z")
        _insert_log(d1, "r2", "2026-04-01T20:00:00.000000Z")
        _insert_log(d1, "r3", "2026-04-02T05:00:00.000000Z")  # different day

        summary = await archive_day(
            d1,
            "2026-04-01",
            dry_run=False,
            bucket="test-bucket",
            endpoint_url="https://test.r2.dev",
            access_key_id="key",
            secret_access_key="secret",
        )

        assert summary["row_count"] == 2
        assert summary["deleted"] == 2
        assert summary["r2_key"] == "logs/2026/04/01.json.gz"

        # Upload was called with compressed data
        mock_upload.assert_awaited_once()
        # _upload_to_r2(data, key, *, bucket=..., ...)
        call_args = mock_upload.call_args
        assert call_args.args[1] == "logs/2026/04/01.json.gz"

        # Only the target day's rows were deleted; r3 remains
        result = d1._conn.execute("SELECT COUNT(*) FROM api_logs").fetchone()
        assert result[0] == 1

    async def test_r2_key_format(self, d1):
        """Verify R2 key path is logs/YYYY/MM/DD.json.gz."""
        from scripts.cloudflare.r2_archive_logs import archive_day

        _insert_log(d1, "r1", "2025-12-25T10:00:00.000000Z")

        summary = await archive_day(
            d1,
            "2025-12-25",
            dry_run=True,
            bucket="b",
            endpoint_url="https://test.r2.dev",
            access_key_id="k",
            secret_access_key="s",
        )

        assert summary["r2_key"] == "logs/2025/12/25.json.gz"
