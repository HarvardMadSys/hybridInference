from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops.db.label_sessions import (
    assign_sessions,
    main,
)


def _row(ts, user_id="u1", user_agent="agent/1.0", model_id="m1", row_id=1):
    return {
        "id": row_id,
        "timestamp": ts,
        "user_id": user_id,
        "user_agent": user_agent,
        "model_id": model_id,
    }


class TestAssignSessionsBasic:
    def test_empty_input_returns_empty(self):
        assert assign_sessions([]) == []

    def test_single_request_gets_one_session(self):
        rows = [_row("2026-01-01T00:00:00Z")]
        result = assign_sessions(rows)
        assert len(result) == 1
        assert result[0].session_id.startswith("sess_")
        assert result[0].row_id == 1

    def test_two_close_requests_same_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:02:00Z", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id == result[1].session_id

    def test_two_far_requests_different_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:15:00Z", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id != result[1].session_id

    def test_different_user_agent_different_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_agent="agent-a", row_id=1),
            _row("2026-01-01T00:00:01Z", user_agent="agent-b", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id != result[1].session_id

    def test_model_switch_same_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", model_id="opus", row_id=1),
            _row("2026-01-01T00:00:05Z", model_id="sonnet", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id == result[1].session_id

    def test_gap_exactly_at_threshold_splits(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:10:00Z", row_id=2),
        ]
        result = assign_sessions(rows, gap_minutes=10.0)
        assert result[0].session_id != result[1].session_id

    def test_gap_just_under_threshold_same_session(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:09:59Z", row_id=2),
        ]
        result = assign_sessions(rows, gap_minutes=10.0)
        assert result[0].session_id == result[1].session_id

    def test_custom_gap_threshold(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:02:00Z", row_id=2),
        ]
        result = assign_sessions(rows, gap_minutes=1.0)
        assert result[0].session_id != result[1].session_id


class TestAssignSessionsMultiUser:
    def test_different_users_different_sessions(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_id="u1", row_id=1),
            _row("2026-01-01T00:00:01Z", user_id="u2", row_id=2),
        ]
        result = assign_sessions(rows)
        assert result[0].session_id != result[1].session_id

    def test_three_sessions_two_users(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_id="u1", row_id=1),
            _row("2026-01-01T00:01:00Z", user_id="u1", row_id=2),
            _row("2026-01-01T00:00:00Z", user_id="u2", row_id=3),
            _row("2026-01-01T01:00:00Z", user_id="u1", row_id=4),
        ]
        result = assign_sessions(rows)
        sess_ids = [r.session_id for r in result]
        assert sess_ids[0] == sess_ids[1]
        assert sess_ids[0] != sess_ids[2]
        assert sess_ids[0] != sess_ids[3]
        assert sess_ids[2] != sess_ids[3]

    def test_preserves_all_rows(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_id="u1", row_id=i)
            for i in range(100)
        ]
        result = assign_sessions(rows)
        assert len(result) == 100
        returned_ids = {r.row_id for r in result}
        assert returned_ids == set(range(100))


class TestSessionIdFormat:
    def test_session_id_format(self):
        rows = [_row("2026-01-01T00:00:00Z")]
        result = assign_sessions(rows)
        sid = result[0].session_id
        assert sid.startswith("sess_")
        parts = sid.split("_")
        assert len(parts) == 3

    def test_session_ids_sequential_within_group(self):
        rows = [
            _row("2026-01-01T00:00:00Z", row_id=1),
            _row("2026-01-01T00:15:00Z", row_id=2),
            _row("2026-01-01T00:30:00Z", row_id=3),
        ]
        result = assign_sessions(rows)
        seqs = [int(r.session_id.split("_")[-1]) for r in result]
        assert seqs == [1, 2, 3]

    def test_session_ids_sequential_across_groups(self):
        rows = [
            _row("2026-01-01T00:00:00Z", user_id="u1", row_id=1),
            _row("2026-01-01T00:00:00Z", user_id="u2", row_id=2),
            _row("2026-01-01T00:15:00Z", user_id="u1", row_id=3),
            _row("2026-01-01T00:15:00Z", user_id="u2", row_id=4),
        ]
        result = assign_sessions(rows)
        u1_sessions = [r for r in result if r.row_id in (1, 3)]
        u2_sessions = [r for r in result if r.row_id in (2, 4)]
        assert int(u1_sessions[0].session_id.split("_")[-1]) == 1
        assert int(u1_sessions[1].session_id.split("_")[-1]) == 2
        assert int(u2_sessions[0].session_id.split("_")[-1]) == 1
        assert int(u2_sessions[1].session_id.split("_")[-1]) == 2


@pytest.fixture
def mock_pool():
    conn = AsyncMock()
    conn.fetch.return_value = [
        {
            "id": 1,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "user_id": "u1",
            "user_agent": "agent/1.0",
            "model_id": "m1",
            "provider": "p1",
            "surface": None,
            "alias_input": None,
            "status_code": 200,
            "latency_ms": 100,
            "ttft_ms": 50,
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "reasoning_tokens": None,
            "total_tokens": 30,
            "cost_usd": None,
            "stream": False,
            "error": None,
        },
        {
            "id": 2,
            "timestamp": "2026-01-01T00:01:00+00:00",
            "user_id": "u1",
            "user_agent": "agent/1.0",
            "model_id": "m1",
            "provider": "p1",
            "surface": None,
            "alias_input": None,
            "status_code": 200,
            "latency_ms": 200,
            "ttft_ms": 80,
            "prompt_tokens": 15,
            "completion_tokens": 25,
            "reasoning_tokens": None,
            "total_tokens": 40,
            "cost_usd": None,
            "stream": False,
            "error": None,
        },
    ]
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    pool.close = AsyncMock()
    return pool, conn


class TestMainExport:
    @pytest.mark.asyncio
    async def test_exports_jsonl_file(self, mock_pool, tmp_path):
        pool, _conn = mock_pool
        out = str(tmp_path / "out.jsonl")
        with patch("ops.db.label_sessions.asyncpg.create_pool", new=AsyncMock(return_value=pool)):
            await main(output_path=out, gap_minutes=10.0)
        with open(out) as f:
            lines = f.readlines()
        assert len(lines) == 2
        import json
        r1 = json.loads(lines[0])
        assert r1["session_id"].startswith("sess_")
        assert r1["id"] == 1

    @pytest.mark.asyncio
    async def test_summary_output(self, mock_pool, tmp_path, capsys):
        pool, _conn = mock_pool
        out = str(tmp_path / "out.jsonl")
        with patch("ops.db.label_sessions.asyncpg.create_pool", new=AsyncMock(return_value=pool)):
            await main(output_path=out, gap_minutes=10.0)
        captured = capsys.readouterr()
        assert "Session labeling summary" in captured.out
        assert "Total requests" in captured.out

    @pytest.mark.asyncio
    async def test_same_session_rows_share_id(self, mock_pool, tmp_path):
        pool, _conn = mock_pool
        out = str(tmp_path / "out.jsonl")
        with patch("ops.db.label_sessions.asyncpg.create_pool", new=AsyncMock(return_value=pool)):
            await main(output_path=out, gap_minutes=10.0)
        import json
        with open(out) as f:
            rows = [json.loads(l) for l in f]
        assert rows[0]["session_id"] == rows[1]["session_id"]
