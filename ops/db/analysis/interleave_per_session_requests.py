"""Merge per-session JSONL files into a controlled-concurrency replay stream."""

import argparse
import heapq
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_QWEN_TRACE_FIELDS = (
    "chat_id",
    "parent_chat_id",
    "timestamp",
    "input_length",
    "output_length",
    "type",
    "turn",
    "hash_ids",
)


@dataclass
class ParsedRow:
    """A validated qwen-trace row plus its computed metadata."""

    line_number: int
    row: dict[str, Any]
    chat_id: Any
    relative_offset_seconds: float
    row_index: int


@dataclass
class LoadedSession:
    """A loaded session file with its queue order index."""

    queue_index: int
    rows: list[ParsedRow]
    last_offset: float
    session_id: int


@dataclass
class ReplayRow:
    """A replay-emitted row with sort-key metadata."""

    timestamp: float
    queue_index: int
    row_index: int
    data: dict[str, Any]


def parse_numeric_timestamp(value: Any) -> float | None:
    """Parse a qwen-trace timestamp as relative offset seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, TypeError):
            return None
    return None


def _is_qwen_trace_row(row: dict[str, Any]) -> bool:
    """Return True when the row has all required qwen-trace fields."""
    return all(field_name in row for field_name in REQUIRED_QWEN_TRACE_FIELDS)


def load_session_file(path: Path, queue_index: int) -> LoadedSession:
    """Load and validate one session-*.jsonl file."""
    raw_rows: list[tuple[int, dict[str, Any], float]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid JSON ({exc})") from None
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number}: JSON value is not an object")
            if not _is_qwen_trace_row(value):
                missing = [f for f in REQUIRED_QWEN_TRACE_FIELDS if f not in value]
                raise ValueError(
                    f"line {line_number}: missing qwen-trace fields: {', '.join(missing)}"
                )
            ts = parse_numeric_timestamp(value.get("timestamp"))
            if ts is None:
                raise ValueError(
                    f"line {line_number}: invalid numeric timestamp: {value.get('timestamp')!r}"
                )
            raw_rows.append((line_number, value, ts))

    if not raw_rows:
        raise ValueError("session file contains no rows")

    # Sort by timestamp, then original line order
    raw_rows.sort(key=lambda r: (r[2], r[0]))

    base_ts = raw_rows[0][2]
    session_id = queue_index + 1
    parsed_rows: list[ParsedRow] = []
    for idx, (line_number, row, ts) in enumerate(raw_rows):
        parsed_rows.append(
            ParsedRow(
                line_number=line_number,
                row=row,
                chat_id=row["chat_id"],
                relative_offset_seconds=ts - base_ts,
                row_index=idx,
            )
        )

    return LoadedSession(
        queue_index=queue_index,
        rows=parsed_rows,
        last_offset=parsed_rows[-1].relative_offset_seconds,
        session_id=session_id,
    )


def schedule_sessions(
    sessions: list[LoadedSession],
    concurrency: int,
) -> dict[int, float]:
    """Apply sliding-window scheduling and return session_start per queue_index."""
    if concurrency <= 0:
        raise ValueError("--concurrency must be > 0")

    session_count = len(sessions)
    starts: dict[int, float] = {}

    if session_count == 0:
        return starts

    # Min-heap of (end_time, queue_index)
    heap: list[tuple[float, int]] = []

    # Seed the first batch
    batch_size = min(concurrency, session_count)
    for i in range(batch_size):
        starts[sessions[i].queue_index] = 0.0
        heapq.heappush(heap, (sessions[i].last_offset, sessions[i].queue_index))

    # Process remaining sessions
    next_index = batch_size
    while heap and next_index < session_count:
        end_time, _freed_queue = heapq.heappop(heap)
        if next_index < session_count:
            starts[sessions[next_index].queue_index] = end_time
            heapq.heappush(
                heap,
                (end_time + sessions[next_index].last_offset, sessions[next_index].queue_index),
            )
            next_index += 1

    return starts


def emit_replay(
    sessions: list[LoadedSession],
    starts: dict[int, float],
) -> list[dict[str, Any]]:
    """Build replay rows and sort by (timestamp, queue_index, row_index)."""
    replay_rows: list[ReplayRow] = []

    for session in sessions:
        session_start = starts.get(session.queue_index, 0.0)
        prev_chat_id: Any = None

        for parsed_row in session.rows:
            row = dict(parsed_row.row)
            row["timestamp"] = session_start + parsed_row.relative_offset_seconds
            row["parent_chat_id"] = -1 if parsed_row.row_index == 0 else prev_chat_id
            prev_chat_id = parsed_row.chat_id

            replay_rows.append(
                ReplayRow(
                    timestamp=row["timestamp"],
                    queue_index=session.queue_index,
                    row_index=parsed_row.row_index,
                    data=row,
                )
            )

    # Sort by rewritten timestamp, then launch order, then row index
    replay_rows.sort(key=lambda r: (r.timestamp, r.queue_index, r.row_index))
    return [rr.data for rr in replay_rows]


def merge_sessions(
    input_dir: Path,
    *,
    concurrency: int,
) -> list[dict[str, Any]]:
    """Load session files, schedule, and emit merged replay rows."""
    session_files = sorted(
        input_dir.glob("session-*.jsonl"),
        key=lambda p: p.name,
    )
    if not session_files:
        raise FileNotFoundError(f"no session-*.jsonl files found in {input_dir}")

    sessions: list[LoadedSession] = []
    for queue_index, path in enumerate(session_files):
        sessions.append(load_session_file(path, queue_index))

    starts = schedule_sessions(sessions, concurrency)
    return emit_replay(sessions, starts)


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Merge per-session JSONL files into a concurrent replay stream."
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing session-*.jsonl files from split_api_logs_sessions.py",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Number of sessions to keep active when possible (default: 4)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Merged JSONL output path (default: stdout)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing output file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the interleave CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.input_dir.is_dir():
        print(f"ERROR: input directory does not exist: {args.input_dir}", file=sys.stderr)
        return 1

    if args.concurrency <= 0:
        print("ERROR: --concurrency must be > 0", file=sys.stderr)
        return 1

    output_path = args.output
    if output_path and output_path.exists() and not args.overwrite:
        print(
            f"ERROR: output file exists: {output_path} (use --overwrite to replace)",
            file=sys.stderr,
        )
        return 1

    try:
        replay_rows = merge_sessions(args.input_dir, concurrency=args.concurrency)
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    total_replayed = len(replay_rows)
    session_files = sorted(
        args.input_dir.glob("session-*.jsonl"),
        key=lambda p: p.name,
    )
    print(f"Merged {len(session_files)} sessions, {total_replayed} rows", file=sys.stderr)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            for row in replay_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str))
                handle.write("\n")
        print(f"Wrote merged replay to {output_path}", file=sys.stderr)
    else:
        for row in replay_rows:
            print(json.dumps(row, ensure_ascii=False, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
