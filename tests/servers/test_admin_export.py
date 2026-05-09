"""Tests for GET /admin/export/requests streaming JSONL endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient


def _make_mock_row(
    *,
    request_id: str = "req-abc",
    user_id: str = "user-1",
    user_name: str = "alice",
    user_email: str = "alice@example.com",
    model_id: str = "gpt-4o",
    provider: str = "openai",
    timestamp: datetime | None = None,
    status_code: int = 200,
    latency_ms: int = 1200,
    ttft_ms: int = 300,
    prompt_tokens: int = 50,
    completion_tokens: int = 100,
    reasoning_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    total_tokens: int = 150,
    cost_usd: Decimal = Decimal("0.00120000"),
    error: str | None = None,
    prompt: str = "Hello",
    response: str = "World",
    tools: Any = None,
    metadata: Any = None,
    request_payload: Any = None,
) -> dict:
    return {
        "request_id": request_id,
        "user_id": user_id,
        "user_name": user_name,
        "user_email": user_email,
        "model_id": model_id,
        "provider": provider,
        "timestamp": timestamp or datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "ttft_ms": ttft_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "error": error,
        "prompt": prompt,
        "response": response,
        "tools": tools,
        "metadata": metadata,
        "request_payload": request_payload,
    }


def _make_mock_db(rows_per_batch: list[list[dict]]):
    """Return a mock db_logger whose pool yields rows_per_batch in successive fetches."""
    mock_db = MagicMock()
    mock_pool = MagicMock()
    mock_db.pool = mock_pool

    fetch_results = iter(rows_per_batch)

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=lambda *a, **kw: next(fetch_results, []))

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)
    mock_db.log_admin_action = AsyncMock()

    return mock_db


def test_export_streams_jsonl():
    """Endpoint returns JSONL with one record per row, no prompt/response by default."""
    from serving.servers.app import app
    from serving.servers.deps import get_db_logger, verify_admin_access

    row = _make_mock_row()
    db = _make_mock_db([[row], []])
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "application/x-ndjson" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]

    lines = [line for line in resp.text.strip().split("\n") if line]
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["request_id"] == "req-abc"
    assert record["model_id"] == "gpt-4o"
    assert record["provider"] == "openai"
    assert record["ttft_ms"] == 300
    assert record["latency_ms"] == 1200
    assert record["prompt_tokens"] == 50
    assert record["completion_tokens"] == 100
    assert "reasoning_tokens" in record  # field present (may be None)
    assert record["total_tokens"] == 150
    assert isinstance(record["cost_usd"], str)
    assert record["cost_usd"] == "0.00120000"
    assert record["status_code"] == 200
    assert record["error"] is None
    assert "prompt" not in record
    assert "response" not in record


def test_export_includes_content_when_requested():
    """With include_content=true, prompt and response appear in each record."""
    from serving.servers.app import app
    from serving.servers.deps import get_db_logger, verify_admin_access

    tools = [{"name": "lookup", "input_schema": {"type": "object"}}]
    metadata = {"surface": "anthropic_messages"}
    request_payload = {
        "model": "claude-test",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Say hi"}],
        "tools": tools,
    }
    row = _make_mock_row(
        prompt="Say hi",
        response="Hi there",
        tools=json.dumps(tools),
        metadata=json.dumps(metadata),
        request_payload=json.dumps(request_payload),
    )
    db = _make_mock_db([[row], []])
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests"
            "?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z"
            "&include_content=true",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line]
    record = json.loads(lines[0])
    assert record["prompt"] == "Say hi"
    assert record["response"] == "Hi there"
    assert record["tools"] == tools
    assert record["metadata"] == metadata
    assert record["request_payload"] == request_payload


def test_export_streams_multiple_batches():
    """Generator continues fetching after a full batch and stops on a partial batch."""
    from serving.servers.app import app
    from serving.servers.deps import get_db_logger, verify_admin_access

    # batch_size in the route is 500; a full batch must equal that size to keep paging.
    full_batch = [_make_mock_row(request_id=f"req-{i}") for i in range(500)]
    partial_batch = [_make_mock_row(request_id=f"req-{i}") for i in range(500, 502)]
    db = _make_mock_db([full_batch, partial_batch])
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line]
    assert len(lines) == 502


def test_export_missing_start_time_returns_422():
    """start_time is required; missing it returns HTTP 422."""
    from serving.servers.app import app
    from serving.servers.deps import get_db_logger, verify_admin_access

    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = MagicMock
    try:
        client = TestClient(app)
        resp = client.get("/admin/export/requests")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_export_no_db_returns_500():
    """Returns 500 when db_logger has no pool."""
    from serving.servers.app import app
    from serving.servers.deps import get_db_logger, verify_admin_access

    no_db = MagicMock()
    no_db.pool = None
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: no_db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z",
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 500


def test_export_applies_user_id_filter():
    """user_id filter is passed through to the SQL query."""
    from serving.servers.app import app
    from serving.servers.deps import get_db_logger, verify_admin_access

    row = _make_mock_row(user_id="user-1")
    db = _make_mock_db([[row], []])
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests"
            "?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z"
            "&user_id=user-1",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["user_id"] == "user-1"
    # Verify mock was actually called (filter was applied)
    mock_conn = db.pool.acquire.return_value.__aenter__.return_value
    call_args = mock_conn.fetch.call_args
    assert call_args is not None
    query = call_args[0][0]
    assert "l.user_id = $3" in query


def test_export_applies_model_id_filter():
    """model_id filter is passed through to the SQL query."""
    from serving.servers.app import app
    from serving.servers.deps import get_db_logger, verify_admin_access

    row = _make_mock_row(model_id="gpt-4o")
    db = _make_mock_db([[row], []])
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests"
            "?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z"
            "&model_id=gpt-4o",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["model_id"] == "gpt-4o"
    # Verify mock was actually called (filter was applied)
    mock_conn = db.pool.acquire.return_value.__aenter__.return_value
    call_args = mock_conn.fetch.call_args
    assert call_args is not None
    query = call_args[0][0]
    assert "l.model_id = $3" in query


def test_export_applies_errors_only_filter():
    """errors_only filter adds an error/status predicate to the SQL query."""
    from serving.servers.app import app
    from serving.servers.deps import get_db_logger, verify_admin_access

    row = _make_mock_row(error="boom", status_code=500)
    db = _make_mock_db([[row], []])
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.dependency_overrides[get_db_logger] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get(
            "/admin/export/requests"
            "?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z"
            "&errors_only=true",
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line]
    assert len(lines) == 1
    # Verify mock was actually called (filter was applied)
    mock_conn = db.pool.acquire.return_value.__aenter__.return_value
    call_args = mock_conn.fetch.call_args
    assert call_args is not None
    query = call_args[0][0]
    assert "l.error IS NOT NULL" in query
