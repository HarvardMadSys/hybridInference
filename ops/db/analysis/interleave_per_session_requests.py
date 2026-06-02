"""Interleave per-session qwen trace requests into one replay stream."""

from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = (
    "chat_id",
    "parent_chat_id",
    "timestamp",
    "input_length",
    "output_length",
    "type",
    "turn",
    "hash_ids",
)


@dataclass(frozen=True)
class SessionRow:
    """One request row from a session trace with its timing offset."""

    line_number: int
    row: dict[str, Any]
    timestamp_seconds: float
    relative_offset_seconds: float


@dataclass(frozen=True)
class LoadedSession:
    """All rows loaded from a single session-*.jsonl file."""

    session_id: int
    source_file: Path
    rows: list[SessionRow]


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Interleave per-session qwen trace JSONL files into one replay stream."
    )
    parser.add_argument("input_dir", type=Path, help="Directory containing session-*.jsonl")
    parser.add_argument("--concurrency", type=int, required=True, help="Active session count")
    parser.add_argument("--output", type=Path, required=True, help="Merged JSONL output path")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing output file")
    return parser


def _parse_timestamp_seconds(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise ValueError(f"invalid timestamp: {value!r}")
        return timestamp
    raise ValueError(f"invalid timestamp: {value!r}")


def _round_seconds(value: float) -> float:
    return round(value, 3)


def _validate_row(row: dict[str, Any]) -> None:
    for field_name in REQUIRED_FIELDS:
        if field_name not in row:
            raise ValueError(f"missing required field: {field_name}")


def _session_end_time(session: LoadedSession, start_time: float) -> float:
    return _round_seconds(start_time + session.rows[-1].relative_offset_seconds)


def load_sessions(input_dir: Path) -> list[LoadedSession]:
    """Load and validate all session-*.jsonl files from a directory."""
    sessions: list[LoadedSession] = []
    for session_id, path in enumerate(sorted(input_dir.glob("session-*.jsonl")), start=1):
        parsed_rows: list[tuple[int, dict[str, Any], float]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError(f"line {line_number} in {path.name} is not an object")
                _validate_row(value)
                parsed_rows.append(
                    (line_number, value, _parse_timestamp_seconds(value["timestamp"]))
                )
        if not parsed_rows:
            raise ValueError(f"empty session file: {path.name}")
        parsed_rows.sort(key=lambda item: (item[2], item[0]))
        first_timestamp = parsed_rows[0][2]
        sessions.append(
            LoadedSession(
                session_id=session_id,
                source_file=path,
                rows=[
                    SessionRow(
                        line_number=line_number,
                        row=row,
                        timestamp_seconds=timestamp_seconds,
                        relative_offset_seconds=_round_seconds(timestamp_seconds - first_timestamp),
                    )
                    for line_number, row, timestamp_seconds in parsed_rows
                ],
            )
        )
    return sessions


def assign_session_start_times(sessions: list[LoadedSession], *, concurrency: int) -> list[float]:
    """Assign a start time to each session bounded by the concurrency limit."""
    if concurrency <= 0:
        raise ValueError("concurrency must be greater than zero")

    if not sessions:
        return []

    start_times: list[float] = [0.0] * len(sessions)
    active_sessions: list[tuple[float, int]] = []

    for session_index, session in enumerate(sessions):
        if session_index < min(concurrency, len(sessions)):
            start_time = 0.0
        else:
            start_time, _ = heapq.heappop(active_sessions)
        start_times[session_index] = start_time
        heapq.heappush(
            active_sessions,
            (_session_end_time(session, start_time), session_index),
        )

    return start_times


def interleave_sessions(sessions: list[LoadedSession], *, concurrency: int) -> list[dict[str, Any]]:
    """Interleave sessions into one timestamp-ordered replay stream."""
    start_times = assign_session_start_times(sessions, concurrency=concurrency)

    replay_rows: list[tuple[float, int, int, dict[str, Any]]] = []
    for session_index, (session, start_time) in enumerate(zip(sessions, start_times, strict=True)):
        previous_chat_id = -1
        for row_index, session_row in enumerate(session.rows):
            replay_row = deepcopy(session_row.row)
            replay_row["timestamp"] = _round_seconds(
                start_time + session_row.relative_offset_seconds
            )
            replay_row["parent_chat_id"] = previous_chat_id
            previous_chat_id = replay_row["chat_id"]
            replay_rows.append((replay_row["timestamp"], session_index, row_index, replay_row))

    replay_rows.sort(key=lambda item: (item[0], item[1], item[2]))
    return [row for _, _, _, row in replay_rows]


def write_output(output_path: Path, rows: list[dict[str, Any]], *, overwrite: bool) -> None:
    """Write replay rows to output_path as JSONL."""
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"output file {output_path} already exists")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: load, interleave, and write the replay stream."""
    args = build_parser().parse_args(argv)

    try:
        if not args.input_dir.is_dir():
            raise FileNotFoundError("input directory does not exist")
        if args.concurrency <= 0:
            raise ValueError("--concurrency must be > 0")

        sessions = load_sessions(args.input_dir)
        if not sessions:
            raise FileNotFoundError("no session-*.jsonl files found")
        rows = interleave_sessions(sessions, concurrency=args.concurrency)
        write_output(args.output, rows, overwrite=args.overwrite)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"Wrote {len(rows)} replay rows from {len(sessions)} sessions to {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
