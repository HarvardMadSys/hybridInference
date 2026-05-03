"""Unit tests for the Cloudflare D1 HTTP client.

All tests mock the HTTP layer — no real D1 calls are made.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from serving.storage.d1_client import (
    MAX_BATCH_STATEMENTS,
    D1Client,
    D1ConnectionError,
    D1QueryError,
    D1Result,
)


@pytest.fixture
def client() -> D1Client:
    """Create a D1Client with dummy credentials."""
    return D1Client(
        account_id="test-account",
        database_id="test-db-id",
        api_token="test-token",
    )


def _mock_response(body: dict[str, Any], status: int = 200) -> AsyncMock:
    """Build a mock aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=json.dumps(body))
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _success_body(rows: list[dict[str, Any]], meta: dict | None = None) -> dict:
    """Build a successful /query response body."""
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": [
            {
                "results": rows,
                "success": True,
                "meta": meta or {"changes": 0, "duration": 0.1, "rows_read": len(rows)},
            }
        ],
    }


def _batch_success_body(results_list: list[dict]) -> dict:
    """Build a successful /raw response body with columnar format."""
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": results_list,
    }


def _error_body(message: str, code: int = 7500) -> dict:
    """Build an error response body."""
    return {
        "success": False,
        "errors": [{"code": code, "message": message}],
        "messages": [],
        "result": [],
    }


# ------------------------------------------------------------------
# query() tests
# ------------------------------------------------------------------


class TestQuery:
    """Tests for D1Client.query()."""

    async def test_simple_select(self, client: D1Client):
        rows = [{"id": 1, "name": "alice"}]
        mock_resp = _mock_response(_success_body(rows))

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=mock_resp)
            mock_session.return_value = session

            result = await client.query("SELECT * FROM users WHERE id = ?", [1])

        assert len(result.rows) == 1
        assert result.rows[0]["name"] == "alice"

    async def test_empty_result(self, client: D1Client):
        mock_resp = _mock_response(_success_body([]))

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=mock_resp)
            mock_session.return_value = session

            result = await client.query("SELECT * FROM users WHERE id = ?", [999])

        assert result.rows == []

    async def test_query_error_raises(self, client: D1Client):
        mock_resp = _mock_response(_error_body("no such table: foo"))

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=mock_resp)
            mock_session.return_value = session

            with pytest.raises(D1QueryError, match="no such table"):
                await client.query("SELECT * FROM foo")

    async def test_non_json_response_raises(self, client: D1Client):
        resp = AsyncMock()
        resp.status = 502
        resp.text = AsyncMock(return_value="<html>Bad Gateway</html>")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=resp)
            mock_session.return_value = session

            with pytest.raises(D1QueryError, match="non-JSON response"):
                await client.query("SELECT 1")

    async def test_connection_error(self, client: D1Client):
        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(side_effect=aiohttp.ClientError("refused"))
            mock_session.return_value = session

            with pytest.raises(D1ConnectionError, match="unreachable"):
                await client.query("SELECT 1")

    async def test_timeout_raises_connection_error(self, client: D1Client):
        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(side_effect=TimeoutError("timed out"))
            mock_session.return_value = session

            with pytest.raises(D1ConnectionError, match="timed out"):
                await client.query("SELECT 1")

    async def test_meta_fields_parsed(self, client: D1Client):
        meta = {
            "changes": 1,
            "last_row_id": 42,
            "duration": 3.14,
            "rows_read": 0,
            "rows_written": 1,
        }
        mock_resp = _mock_response(_success_body([], meta=meta))

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=mock_resp)
            mock_session.return_value = session

            result = await client.execute("INSERT INTO t VALUES (?)", [1])

        assert result.changes == 1
        assert result.last_row_id == 42
        assert result.duration_ms == 3.14
        assert result.rows_written == 1

    async def test_params_sent_in_body(self, client: D1Client):
        mock_resp = _mock_response(_success_body([]))

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=mock_resp)
            mock_session.return_value = session

            await client.query("SELECT * FROM t WHERE a = ? AND b = ?", ["x", 42])

        call_kwargs = session.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["params"] == ["x", 42]


# ------------------------------------------------------------------
# batch() tests
# ------------------------------------------------------------------


class TestBatch:
    """Tests for D1Client.batch()."""

    async def test_batch_parses_columnar_format(self, client: D1Client):
        raw_result = {
            "results": {"columns": ["id", "name"], "rows": [[1, "alice"], [2, "bob"]]},
            "success": True,
            "meta": {"changes": 0, "duration": 0.1, "rows_read": 2, "rows_written": 0},
        }
        mock_resp = _mock_response(_batch_success_body([raw_result]))

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=mock_resp)
            mock_session.return_value = session

            results = await client.batch([("SELECT * FROM users", None)])

        assert len(results) == 1
        assert len(results[0].rows) == 2
        assert results[0].rows[0] == {"id": 1, "name": "alice"}
        assert results[0].rows[1] == {"id": 2, "name": "bob"}

    async def test_batch_exceeding_limit_raises(self, client: D1Client):
        statements = [("SELECT 1", None)] * (MAX_BATCH_STATEMENTS + 1)
        with pytest.raises(ValueError, match="exceeds limit"):
            await client.batch(statements)

    async def test_batch_error_raises(self, client: D1Client):
        mock_resp = _mock_response(_error_body("UNIQUE constraint failed"))

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=mock_resp)
            mock_session.return_value = session

            with pytest.raises(D1QueryError, match="UNIQUE constraint"):
                await client.batch(
                    [
                        ("INSERT INTO t VALUES (?)", [1]),
                        ("INSERT INTO t VALUES (?)", [1]),
                    ]
                )

    async def test_batch_multiple_statements(self, client: D1Client):
        results = [
            {
                "results": {"columns": [], "rows": []},
                "success": True,
                "meta": {"changes": 1, "duration": 0.05, "rows_written": 1},
            },
            {
                "results": {"columns": [], "rows": []},
                "success": True,
                "meta": {"changes": 1, "duration": 0.03, "rows_written": 1},
            },
        ]
        mock_resp = _mock_response(_batch_success_body(results))

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=mock_resp)
            mock_session.return_value = session

            parsed = await client.batch(
                [
                    ("INSERT INTO a VALUES (?)", [1]),
                    ("INSERT INTO b VALUES (?)", [2]),
                ]
            )

        assert len(parsed) == 2
        assert parsed[0].changes == 1
        assert parsed[1].changes == 1


# ------------------------------------------------------------------
# health_check() tests
# ------------------------------------------------------------------


class TestHealthCheck:
    """Tests for D1Client.health_check()."""

    async def test_healthy(self, client: D1Client):
        mock_resp = _mock_response(_success_body([{"ok": 1}]))

        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(return_value=mock_resp)
            mock_session.return_value = session

            assert await client.health_check() is True

    async def test_unhealthy_on_error(self, client: D1Client):
        with patch.object(client, "_ensure_session") as mock_session:
            session = AsyncMock()
            session.post = MagicMock(side_effect=aiohttp.ClientError("down"))
            mock_session.return_value = session

            assert await client.health_check() is False


# ------------------------------------------------------------------
# _parse_columnar() tests
# ------------------------------------------------------------------


class TestParseColumnar:
    """Tests for the columnar → dict row parser."""

    def test_normal_case(self):
        raw = {"columns": ["a", "b"], "rows": [[1, 2], [3, 4]]}
        result = D1Client._parse_columnar(raw)
        assert result == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]

    def test_empty_rows(self):
        raw = {"columns": ["a"], "rows": []}
        assert D1Client._parse_columnar(raw) == []

    def test_empty_columns(self):
        raw = {"columns": [], "rows": []}
        assert D1Client._parse_columnar(raw) == []

    def test_list_input_passthrough(self):
        data = [{"a": 1}]
        assert D1Client._parse_columnar(data) == data

    def test_non_dict_returns_empty(self):
        assert D1Client._parse_columnar(None) == []
        assert D1Client._parse_columnar(42) == []


# ------------------------------------------------------------------
# D1Result dataclass
# ------------------------------------------------------------------


class TestD1Result:
    """Tests for D1Result defaults."""

    def test_defaults(self):
        r = D1Result()
        assert r.rows == []
        assert r.changes == 0
        assert r.last_row_id == 0
        assert r.duration_ms == 0.0

    def test_error_code(self):
        err = D1QueryError("bad sql", code=7500)
        assert err.code == 7500
        assert "bad sql" in str(err)


# ------------------------------------------------------------------
# URL construction
# ------------------------------------------------------------------


class TestURLConstruction:
    """Verify the base URL is built correctly."""

    def test_base_url_format(self, client: D1Client):
        expected = (
            "https://api.cloudflare.com/client/v4/accounts/test-account/d1/database/test-db-id"
        )
        assert client._base_url == expected
