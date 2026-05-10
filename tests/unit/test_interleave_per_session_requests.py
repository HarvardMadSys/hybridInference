"""Unit tests for interleave_per_session_requests."""

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from ops.db.analysis.interleave_per_session_requests import (
    LoadedSession,
    ParsedRow,
    emit_replay,
    load_session_file,
    merge_sessions,
    parse_numeric_timestamp,
    schedule_sessions,
)


def _make_session_dir(files: dict[str, str]) -> Path:
    """Create a temp directory with session JSONL files."""
    tmp = tempfile.mkdtemp()
    path = Path(tmp)
    for name, content in files.items():
        (path / name).write_text(content, encoding="utf-8")
    return path


def _row(chat_id: int, timestamp: float = 0.0, parent_chat_id: int = -1) -> dict:
    """Build a minimal qwen-trace row."""
    return {
        "chat_id": chat_id,
        "parent_chat_id": parent_chat_id,
        "timestamp": timestamp,
        "input_length": 100,
        "output_length": 10,
        "type": "text",
        "turn": 1,
        "hash_ids": [1, 2, 3],
    }


class TestParseNumericTimestamp(TestCase):
    def test_int_timestamp(self):
        self.assertEqual(parse_numeric_timestamp(42), 42.0)

    def test_float_timestamp(self):
        self.assertAlmostEqual(parse_numeric_timestamp(3.14), 3.14)

    def test_zero(self):
        self.assertEqual(parse_numeric_timestamp(0), 0.0)

    def test_negative(self):
        self.assertEqual(parse_numeric_timestamp(-1.5), -1.5)

    def test_string_timestamp(self):
        self.assertEqual(parse_numeric_timestamp("5.5"), 5.5)

    def test_string_int(self):
        self.assertEqual(parse_numeric_timestamp("42"), 42.0)

    def test_empty_string(self):
        self.assertIsNone(parse_numeric_timestamp(""))

    def test_non_numeric_string(self):
        self.assertIsNone(parse_numeric_timestamp("hello"))

    def test_none(self):
        self.assertIsNone(parse_numeric_timestamp(None))

    def test_bool_rejected(self):
        self.assertIsNone(parse_numeric_timestamp(True))


class TestSessionLoading(TestCase):
    def test_single_session_single_row(self):
        content = json.dumps(_row(1, 0.0)) + "\n"
        tmpdir = _make_session_dir({"session-000001.jsonl": content})
        session = load_session_file(Path(tmpdir) / "session-000001.jsonl", 0)

        self.assertEqual(session.session_id, 1)
        self.assertEqual(len(session.rows), 1)
        self.assertEqual(session.rows[0].relative_offset_seconds, 0.0)
        self.assertEqual(session.last_offset, 0.0)

    def test_single_session_preserves_relative_timing(self):
        content = (
            json.dumps(_row(1, 0.0))
            + "\n"
            + json.dumps(_row(2, 5.0))
            + "\n"
            + json.dumps(_row(3, 10.0))
            + "\n"
        )
        tmpdir = _make_session_dir({"session-000001.jsonl": content})
        session = load_session_file(Path(tmpdir) / "session-000001.jsonl", 0)

        self.assertEqual(len(session.rows), 3)
        self.assertEqual(session.rows[0].relative_offset_seconds, 0.0)
        self.assertEqual(session.rows[1].relative_offset_seconds, 5.0)
        self.assertEqual(session.rows[2].relative_offset_seconds, 10.0)
        self.assertEqual(session.last_offset, 10.0)

    def test_session_rows_sorted_by_timestamp(self):
        content = (
            json.dumps(_row(3, 10.0))
            + "\n"
            + json.dumps(_row(1, 0.0))
            + "\n"
            + json.dumps(_row(2, 5.0))
            + "\n"
        )
        tmpdir = _make_session_dir({"session-000001.jsonl": content})
        session = load_session_file(Path(tmpdir) / "session-000001.jsonl", 0)

        ids = [r.chat_id for r in session.rows]
        self.assertEqual(ids, [1, 2, 3])

    def test_empty_session_file_raises(self):
        tmpdir = _make_session_dir({"session-000001.jsonl": ""})
        with self.assertRaises(ValueError) as ctx:
            load_session_file(Path(tmpdir) / "session-000001.jsonl", 0)
        self.assertIn("no rows", str(ctx.exception))

    def test_invalid_json_raises(self):
        tmpdir = _make_session_dir({"session-000001.jsonl": "not json\n"})
        with self.assertRaises(ValueError) as ctx:
            load_session_file(Path(tmpdir) / "session-000001.jsonl", 0)
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_missing_required_fields_raises(self):
        content = json.dumps({"chat_id": 1, "timestamp": 0.0}) + "\n"
        tmpdir = _make_session_dir({"session-000001.jsonl": content})
        with self.assertRaises(ValueError) as ctx:
            load_session_file(Path(tmpdir) / "session-000001.jsonl", 0)
        self.assertIn("missing qwen-trace", str(ctx.exception))

    def test_invalid_timestamp_raises(self):
        row = _row(1, 0.0)
        row["timestamp"] = "not_a_number"
        content = json.dumps(row) + "\n"
        tmpdir = _make_session_dir({"session-000001.jsonl": content})
        with self.assertRaises(ValueError) as ctx:
            load_session_file(Path(tmpdir) / "session-000001.jsonl", 0)
        self.assertIn("invalid numeric timestamp", str(ctx.exception))


class TestScheduling(TestCase):
    def test_concurrency_one_serial(self):
        sessions = [
            LoadedSession(queue_index=0, rows=[], last_offset=5.0, session_id=1),
            LoadedSession(queue_index=1, rows=[], last_offset=10.0, session_id=2),
            LoadedSession(queue_index=2, rows=[], last_offset=3.0, session_id=3),
        ]
        starts = schedule_sessions(sessions, concurrency=1)

        self.assertEqual(starts[0], 0.0)
        self.assertEqual(starts[1], 5.0)
        self.assertEqual(starts[2], 15.0)

    def test_concurrency_all_start_zero(self):
        sessions = [
            LoadedSession(queue_index=0, rows=[], last_offset=10.0, session_id=1),
            LoadedSession(queue_index=1, rows=[], last_offset=5.0, session_id=2),
        ]
        starts = schedule_sessions(sessions, concurrency=10)

        self.assertEqual(starts[0], 0.0)
        self.assertEqual(starts[1], 0.0)

    def test_concurrency_greater_than_sessions(self):
        sessions = [
            LoadedSession(queue_index=0, rows=[], last_offset=5.0, session_id=1),
        ]
        starts = schedule_sessions(sessions, concurrency=5)

        self.assertEqual(starts[0], 0.0)

    def test_sliding_window(self):
        # Session 0: ends at 10, Session 1: ends at 3, Session 2: ends at 7
        sessions = [
            LoadedSession(queue_index=0, rows=[], last_offset=10.0, session_id=1),
            LoadedSession(queue_index=1, rows=[], last_offset=3.0, session_id=2),
            LoadedSession(queue_index=2, rows=[], last_offset=7.0, session_id=3),
        ]
        starts = schedule_sessions(sessions, concurrency=2)

        # First 2 start at 0
        self.assertEqual(starts[0], 0.0)
        self.assertEqual(starts[1], 0.0)
        # Session 1 ends at 3.0, so session 2 starts at 3.0
        self.assertEqual(starts[2], 3.0)

    def test_zero_concurrency_raises(self):
        sessions = [
            LoadedSession(queue_index=0, rows=[], last_offset=5.0, session_id=1),
        ]
        with self.assertRaises(ValueError):
            schedule_sessions(sessions, concurrency=0)

    def test_empty_sessions(self):
        starts = schedule_sessions([], concurrency=2)
        self.assertEqual(starts, {})


class TestEmitReplay(TestCase):
    def test_first_row_parent_is_minus_one(self):
        session = LoadedSession(
            queue_index=0,
            rows=[
                ParsedRow(
                    line_number=1,
                    row=_row(1, 0.0),
                    chat_id=1,
                    relative_offset_seconds=0.0,
                    row_index=0,
                ),
                ParsedRow(
                    line_number=2,
                    row=_row(2, 5.0),
                    chat_id=2,
                    relative_offset_seconds=5.0,
                    row_index=1,
                ),
            ],
            last_offset=5.0,
            session_id=1,
        )
        replay = emit_replay([session], {0: 0.0})

        self.assertEqual(len(replay), 2)
        self.assertEqual(replay[0]["parent_chat_id"], -1)
        self.assertEqual(replay[1]["parent_chat_id"], 1)

    def test_parent_chain_across_three_rows(self):
        rows_data = [
            ParsedRow(
                line_number=1, row=_row(1, 0.0), chat_id=1, relative_offset_seconds=0.0, row_index=0
            ),
            ParsedRow(
                line_number=2, row=_row(2, 5.0), chat_id=2, relative_offset_seconds=5.0, row_index=1
            ),
            ParsedRow(
                line_number=3,
                row=_row(3, 10.0),
                chat_id=3,
                relative_offset_seconds=10.0,
                row_index=2,
            ),
        ]
        session = LoadedSession(
            queue_index=0,
            rows=rows_data,
            last_offset=10.0,
            session_id=1,
        )
        replay = emit_replay([session], {0: 0.0})

        self.assertEqual(len(replay), 3)
        self.assertEqual(replay[0]["parent_chat_id"], -1)
        self.assertEqual(replay[1]["parent_chat_id"], 1)
        self.assertEqual(replay[2]["parent_chat_id"], 2)

    def test_timestamps_are_replay_relative(self):
        session = LoadedSession(
            queue_index=0,
            rows=[
                ParsedRow(
                    line_number=1,
                    row=_row(1, 0.0),
                    chat_id=1,
                    relative_offset_seconds=0.0,
                    row_index=0,
                ),
                ParsedRow(
                    line_number=2,
                    row=_row(2, 5.0),
                    chat_id=2,
                    relative_offset_seconds=5.0,
                    row_index=1,
                ),
            ],
            last_offset=5.0,
            session_id=1,
        )
        # Session starts at 0, so timestamps are just the relative offsets
        replay = emit_replay([session], {0: 0.0})
        self.assertAlmostEqual(replay[0]["timestamp"], 0.0)
        self.assertAlmostEqual(replay[1]["timestamp"], 5.0)

    def test_chained_session_timestamps(self):
        # Session 0: start=0, offsets [0, 5]  -> timestamps [0, 5]
        # Session 1: start=5, offsets [0, 3]  -> timestamps [5, 8]
        session0 = LoadedSession(
            queue_index=0,
            rows=[
                ParsedRow(
                    line_number=1,
                    row=_row(1, 0.0),
                    chat_id=1,
                    relative_offset_seconds=0.0,
                    row_index=0,
                ),
                ParsedRow(
                    line_number=2,
                    row=_row(2, 5.0),
                    chat_id=2,
                    relative_offset_seconds=5.0,
                    row_index=1,
                ),
            ],
            last_offset=5.0,
            session_id=1,
        )
        session1 = LoadedSession(
            queue_index=1,
            rows=[
                ParsedRow(
                    line_number=1,
                    row=_row(3, 0.0),
                    chat_id=3,
                    relative_offset_seconds=0.0,
                    row_index=0,
                ),
                ParsedRow(
                    line_number=2,
                    row=_row(4, 3.0),
                    chat_id=4,
                    relative_offset_seconds=3.0,
                    row_index=1,
                ),
            ],
            last_offset=3.0,
            session_id=2,
        )

        replay = emit_replay([session0, session1], {0: 0.0, 1: 5.0})
        self.assertEqual(len(replay), 4)
        self.assertAlmostEqual(replay[0]["timestamp"], 0.0)
        self.assertAlmostEqual(replay[1]["timestamp"], 5.0)
        self.assertAlmostEqual(replay[2]["timestamp"], 5.0)
        self.assertAlmostEqual(replay[3]["timestamp"], 8.0)

    def test_deterministic_tie_breaking(self):
        # Two sessions with same relative timing, same start time -> queue_index tiebreak
        session0 = LoadedSession(
            queue_index=0,
            rows=[
                ParsedRow(
                    line_number=1,
                    row=_row(1, 0.0),
                    chat_id=1,
                    relative_offset_seconds=0.0,
                    row_index=0,
                ),
            ],
            last_offset=0.0,
            session_id=1,
        )
        session1 = LoadedSession(
            queue_index=1,
            rows=[
                ParsedRow(
                    line_number=1,
                    row=_row(2, 0.0),
                    chat_id=2,
                    relative_offset_seconds=0.0,
                    row_index=0,
                ),
            ],
            last_offset=0.0,
            session_id=2,
        )

        replay = emit_replay([session0, session1], {0: 0.0, 1: 0.0})
        self.assertEqual(len(replay), 2)
        # Same timestamp, so queue_index 0 comes before 1
        self.assertEqual(replay[0]["chat_id"], 1)
        self.assertEqual(replay[1]["chat_id"], 2)


class TestMergeSessions(TestCase):
    def test_single_session_preserves_timing(self):
        content = (
            json.dumps(_row(1, 0.0))
            + "\n"
            + json.dumps(_row(2, 5.0))
            + "\n"
            + json.dumps(_row(3, 10.0))
            + "\n"
        )
        tmpdir = _make_session_dir({"session-000001.jsonl": content})
        replay = merge_sessions(tmpdir, concurrency=4)

        self.assertEqual(len(replay), 3)
        self.assertAlmostEqual(replay[0]["timestamp"], 0.0)
        self.assertAlmostEqual(replay[1]["timestamp"], 5.0)
        self.assertAlmostEqual(replay[2]["timestamp"], 10.0)

    def test_concurrency_one_serial(self):
        content1 = json.dumps(_row(1, 0.0)) + "\n"
        content2 = json.dumps(_row(2, 0.0)) + "\n"
        content3 = json.dumps(_row(3, 0.0)) + "\n"
        tmpdir = _make_session_dir(
            {
                "session-000001.jsonl": content1,
                "session-000002.jsonl": content2,
                "session-000003.jsonl": content3,
            }
        )
        replay = merge_sessions(tmpdir, concurrency=1)

        # All start at 0, timestamps all 0.0, but order is deterministic
        self.assertEqual(len(replay), 3)
        # All same timestamp, so order is by queue_index (session lex order)
        self.assertEqual(replay[0]["chat_id"], 1)
        self.assertEqual(replay[1]["chat_id"], 2)
        self.assertEqual(replay[2]["chat_id"], 3)

    def test_concurrency_all_zero_latency(self):
        """All sessions have last_offset=0, should all start at 0."""
        content = json.dumps(_row(1, 0.0)) + "\n"
        tmpdir = _make_session_dir(
            {
                "session-000001.jsonl": content,
                "session-000002.jsonl": content,
            }
        )
        replay = merge_sessions(tmpdir, concurrency=2)
        self.assertEqual(len(replay), 2)
        self.assertEqual(replay[0]["timestamp"], 0.0)
        self.assertEqual(replay[1]["timestamp"], 0.0)

    def test_nonexistent_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            merge_sessions(Path("/nonexistent"), concurrency=4)

    def test_no_session_files_raises(self):
        tmpdir = _make_session_dir({})
        with self.assertRaises(FileNotFoundError) as ctx:
            merge_sessions(tmpdir, concurrency=4)
        self.assertIn("no session", str(ctx.exception))

    def test_ignores_non_session_files(self):
        content1 = json.dumps(_row(1, 0.0)) + "\n"
        tmpdir = _make_session_dir(
            {
                "session-000001.jsonl": content1,
                "manifest.json": "{}",
                "unclassified.jsonl": json.dumps(_row(99, 0.0)) + "\n",
            }
        )
        replay = merge_sessions(tmpdir, concurrency=2)
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["chat_id"], 1)

    def test_lexical_file_ordering(self):
        """Files loaded in lexical filename order for deterministic queue."""
        content = json.dumps(_row(99, 0.0)) + "\n"
        tmpdir = _make_session_dir(
            {
                "session-000002.jsonl": content,
                "session-000001.jsonl": content,
            }
        )
        replay = merge_sessions(tmpdir, concurrency=2)
        # session-000001 loads first (queue_index=0), has chat_id=99
        # session-000002 loads second (queue_index=1), has chat_id=99
        # Both start at 0, tiebreak by queue_index
        self.assertEqual(replay[0]["chat_id"], 99)
        self.assertEqual(replay[1]["chat_id"], 99)

    def test_qwen_trace_fields_preserved(self):
        row = _row(1, 0.0, -1)
        row["input_length"] = 5000
        row["output_length"] = 250
        row["type"] = "chat"
        row["turn"] = 2
        row["hash_ids"] = [100, 200, 300, 400]
        content = json.dumps(row) + "\n"
        tmpdir = _make_session_dir({"session-000001.jsonl": content})
        replay = merge_sessions(tmpdir, concurrency=2)

        self.assertEqual(replay[0]["input_length"], 5000)
        self.assertEqual(replay[0]["output_length"], 250)
        self.assertEqual(replay[0]["type"], "chat")
        self.assertEqual(replay[0]["turn"], 2)
        self.assertEqual(replay[0]["hash_ids"], [100, 200, 300, 400])
        self.assertEqual(replay[0]["parent_chat_id"], -1)


class TestChatIdPreservation(TestCase):
    def test_chat_ids_unchanged(self):
        content = json.dumps(_row(42, 0.0)) + "\n" + json.dumps(_row(43, 5.0)) + "\n"
        tmpdir = _make_session_dir({"session-000001.jsonl": content})
        replay = merge_sessions(tmpdir, concurrency=2)

        self.assertEqual(replay[0]["chat_id"], 42)
        self.assertEqual(replay[1]["chat_id"], 43)


class TestDeterministicOutput(TestCase):
    def test_repeated_runs_identical(self):
        content1 = json.dumps(_row(1, 0.0)) + "\n"
        content2 = json.dumps(_row(2, 0.0)) + "\n"
        content3 = json.dumps(_row(3, 0.0)) + "\n"
        tmpdir = _make_session_dir(
            {
                "session-000001.jsonl": content1,
                "session-000002.jsonl": content2,
                "session-000003.jsonl": content3,
            }
        )

        replay1 = merge_sessions(tmpdir, concurrency=2)
        replay2 = merge_sessions(tmpdir, concurrency=2)

        self.assertEqual(replay1, replay2)


class TestMultipleSessionsParentChain(TestCase):
    def test_independent_parent_chains(self):
        """Each session has its own parent chain starting from -1."""
        content1 = json.dumps(_row(1, 0.0)) + "\n" + json.dumps(_row(2, 5.0)) + "\n"
        content2 = json.dumps(_row(10, 0.0)) + "\n" + json.dumps(_row(11, 3.0)) + "\n"
        tmpdir = _make_session_dir(
            {
                "session-000001.jsonl": content1,
                "session-000002.jsonl": content2,
            }
        )
        replay = merge_sessions(tmpdir, concurrency=2)

        # Session 0: start=0, Session 1: start=0 (concurrency=10)
        # Chat ID 1 and 10 both start at queue 0 and 1
        # Find rows by chat_id and check parent chains
        by_chat = {r["chat_id"]: r for r in replay}
        self.assertEqual(by_chat[1]["parent_chat_id"], -1)
        self.assertEqual(by_chat[2]["parent_chat_id"], 1)
        self.assertEqual(by_chat[10]["parent_chat_id"], -1)
        self.assertEqual(by_chat[11]["parent_chat_id"], 10)
