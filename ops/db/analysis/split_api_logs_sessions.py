"""Split exported API log JSONL rows into inferred prompt-thread sessions.

Algorithm (hash_ids based):
- Maintain an LRU of the most recent ``window_size`` sessions, keyed by their
  most recent request's ``hash_ids``.
- For each new row, compute the longest common prefix length against every
  session in the window. Score = LCP / max(len_row, len_session_last).
- If the best score >= ``match_threshold``, append the row to that session
  and rewrite ``parent_chat_id`` to the session's previous chat_id.
- Otherwise start a new session; ``parent_chat_id`` is left at -1.
- The newly updated/created session becomes the most-recent in the LRU. When
  the window is full, the least-recently-updated session is evicted (its rows
  are still kept in the final output).
"""

import argparse
import contextlib
import json
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path("per_session")


@dataclass(frozen=True)
class InvalidLine:
    """Diagnostic for a JSONL line that could not become a row."""

    line_number: int
    error: str


@dataclass(frozen=True)
class LogRow:
    """Valid API log row plus stable ordering metadata."""

    line_number: int
    row: dict[str, Any]
    chat_id: int | None
    hash_seq: tuple[int, ...]
    timestamp: datetime | None


@dataclass(frozen=True)
class RowReadResult:
    """Result of reading a JSONL export file."""

    rows: list[LogRow]
    invalid_lines: list[InvalidLine]
    total_lines: int


@dataclass
class Session:
    """Inferred prompt-thread session."""

    session_id: int
    rows: list[LogRow] = field(default_factory=list)
    last_chat_id: int | None = None
    last_hash_seq: tuple[int, ...] = ()
    last_timestamp: datetime | None = None
    updated_order: int = 0


@dataclass(frozen=True)
class SplitResult:
    """Complete result of splitting rows into sessions."""

    sessions: list[Session]
    unclassified: list[LogRow]
    invalid_lines: list[InvalidLine]
    total_lines: int


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an exported timestamp value when possible."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coerce_hash_seq(value: Any) -> tuple[int, ...] | None:
    """Convert a raw hash_ids list into an integer tuple."""
    if not isinstance(value, list) or not value:
        return None
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        out.append(item)
    return tuple(out) if out else None


def _coerce_chat_id(value: Any) -> int | None:
    """Convert chat_id to int when possible."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def read_jsonl_rows(path: Path) -> RowReadResult:
    """Read JSONL rows and collect invalid-line diagnostics."""
    rows: list[LogRow] = []
    invalid_lines: list[InvalidLine] = []
    total_lines = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            total_lines = line_number
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                invalid_lines.append(InvalidLine(line_number, "invalid JSON"))
                continue
            if not isinstance(value, dict):
                invalid_lines.append(InvalidLine(line_number, "JSON value is not an object"))
                continue
            rows.append(
                LogRow(
                    line_number=line_number,
                    row=value,
                    chat_id=_coerce_chat_id(value.get("chat_id")),
                    hash_seq=_coerce_hash_seq(value.get("hash_ids")) or (),
                    timestamp=_parse_timestamp(value.get("timestamp")),
                )
            )
    return RowReadResult(rows=rows, invalid_lines=invalid_lines, total_lines=total_lines)


def _lcp_len(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """Return the length of the longest common prefix between two sequences."""
    limit = min(len(left), len(right))
    i = 0
    while i < limit and left[i] == right[i]:
        i += 1
    return i


def _match_score(row_seq: tuple[int, ...], session_seq: tuple[int, ...]) -> float:
    """Return LCP / max(len_row, len_session)."""
    if not row_seq or not session_seq:
        return 0.0
    denom = max(len(row_seq), len(session_seq))
    if denom == 0:
        return 0.0
    return _lcp_len(row_seq, session_seq) / denom


def _sort_rows(rows: list[LogRow]) -> list[LogRow]:
    """Sort rows by timestamp when present, then original input order."""
    return sorted(
        rows,
        key=lambda row: (
            row.timestamp is None,
            row.timestamp or datetime.max.replace(tzinfo=timezone.utc),
            row.line_number,
        ),
    )


def _select_candidate(
    row_seq: tuple[int, ...],
    window: deque[Session],
    *,
    match_threshold: float,
) -> Session | None:
    """Return the best-matching session in the LRU window, if any qualifies."""
    best: Session | None = None
    best_score = -1.0
    best_lcp = -1
    for session in window:
        score = _match_score(row_seq, session.last_hash_seq)
        if score < match_threshold:
            continue
        lcp = _lcp_len(row_seq, session.last_hash_seq)
        if (
            score > best_score
            or (score == best_score and lcp > best_lcp)
            or (
                score == best_score
                and lcp == best_lcp
                and best is not None
                and session.updated_order > best.updated_order
            )
        ):
            best = session
            best_score = score
            best_lcp = lcp
    return best


def assign_sessions(
    rows: list[LogRow],
    *,
    window_size: int,
    match_threshold: float,
    invalid_lines: list[InvalidLine] | None = None,
    total_lines: int | None = None,
) -> SplitResult:
    """Assign rows into sessions using an LRU window of recent sessions."""
    sessions: list[Session] = []
    unclassified: list[LogRow] = []
    window: deque[Session] = deque()
    update_order = 0
    for row in _sort_rows(rows):
        if not row.hash_seq:
            unclassified.append(row)
            continue
        candidate = _select_candidate(row.hash_seq, window, match_threshold=match_threshold)
        if candidate is None:
            candidate = Session(session_id=len(sessions) + 1)
            sessions.append(candidate)
        else:
            row.row["parent_chat_id"] = candidate.last_chat_id if candidate.last_chat_id is not None else -1
        update_order += 1
        candidate.rows.append(row)
        candidate.last_chat_id = row.chat_id
        candidate.last_hash_seq = row.hash_seq
        candidate.last_timestamp = row.timestamp
        candidate.updated_order = update_order
        with contextlib.suppress(ValueError):
            window.remove(candidate)
        window.append(candidate)
        while len(window) > window_size:
            window.popleft()
    return SplitResult(
        sessions=sessions,
        unclassified=unclassified,
        invalid_lines=invalid_lines or [],
        total_lines=total_lines if total_lines is not None else len(rows),
    )


def _write_jsonl(path: Path, rows: list[LogRow]) -> None:
    """Write (possibly mutated) row objects to JSONL."""
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row.row, ensure_ascii=False, default=str))
            handle.write("\n")


def _iso(value: datetime | None) -> str | None:
    """Return ISO text for a datetime or None."""
    return value.isoformat() if value is not None else None


def _percentile(ordered_values: list[int], percentile: float) -> int:
    """Return nearest-rank style percentile used by analysis summaries."""
    index = int((len(ordered_values) - 1) * percentile + 0.5)
    return ordered_values[index]


def _print_split_stats(
    result: SplitResult,
    *,
    match_threshold: float,
    window_size: int,
) -> None:
    """Print a concise post-run summary for the splitter CLI."""
    classified_rows = sum(len(session.rows) for session in result.sessions)
    valid_rows = classified_rows + len(result.unclassified)
    print(
        f"Stats: total_lines={result.total_lines} valid_rows={valid_rows} "
        f"invalid_lines={len(result.invalid_lines)} classified_rows={classified_rows} "
        f"unclassified_rows={len(result.unclassified)}",
        file=sys.stderr,
    )
    session_sizes = sorted(len(session.rows) for session in result.sessions)
    if not session_sizes:
        print("Stats: session_rows no sessions", file=sys.stderr)
    else:
        print(
            "Stats: session_rows "
            f"min={session_sizes[0]} "
            f"p50={_percentile(session_sizes, 0.50)} "
            f"p90={_percentile(session_sizes, 0.90)} "
            f"p95={_percentile(session_sizes, 0.95)} "
            f"p99={_percentile(session_sizes, 0.99)} "
            f"max={session_sizes[-1]} "
            f"mean={sum(session_sizes) / len(session_sizes):.2f}",
            file=sys.stderr,
        )
    print(
        f"Stats: thresholds match_threshold={match_threshold:g} window_size={window_size}",
        file=sys.stderr,
    )


def _is_generated_output(path: Path) -> bool:
    """Return True for files this splitter may safely replace."""
    return (
        path.name == "unclassified.jsonl"
        or path.name == "manifest.json"
        or path.match("session-*.jsonl")
    )


def _manifest(
    result: SplitResult,
    *,
    source_path: Path,
    window_size: int,
    match_threshold: float,
) -> dict[str, Any]:
    """Build a manifest document for one splitter run."""
    valid_rows = sum(len(session.rows) for session in result.sessions) + len(result.unclassified)
    sessions: list[dict[str, Any]] = []
    for session in result.sessions:
        timestamps = [row.timestamp for row in session.rows if row.timestamp is not None]
        sessions.append(
            {
                "session_id": session.session_id,
                "file": f"session-{session.session_id:06d}.jsonl",
                "row_count": len(session.rows),
                "first_timestamp": _iso(min(timestamps)) if timestamps else None,
                "last_timestamp": _iso(max(timestamps)) if timestamps else None,
                "source_line_numbers": [row.line_number for row in session.rows],
                "chat_ids": [row.chat_id for row in session.rows],
            }
        )
    return {
        "source": str(source_path),
        "total_lines": result.total_lines,
        "valid_rows": valid_rows,
        "invalid_line_count": len(result.invalid_lines),
        "invalid_lines": [
            {"line_number": item.line_number, "error": item.error} for item in result.invalid_lines
        ],
        "unclassified_rows": len(result.unclassified),
        "session_count": len(result.sessions),
        "sessions": sessions,
        "thresholds": {
            "window_size": window_size,
            "match_threshold": match_threshold,
        },
    }


def write_split_result(
    result: SplitResult,
    *,
    output_dir: Path,
    source_path: Path,
    overwrite: bool,
    write_manifest: bool,
    window_size: int,
    match_threshold: float,
) -> None:
    """Write session JSONL files, unclassified rows, and optional manifest."""
    if output_dir.exists() and not output_dir.is_dir():
        raise FileExistsError(f"output path {output_dir} exists and is not a directory")
    if overwrite and source_path.resolve().is_relative_to(output_dir.resolve()):
        raise FileExistsError("source file is inside the output directory")
    if output_dir.exists() and any(output_dir.iterdir()):
        if not overwrite:
            raise FileExistsError(f"output directory {output_dir} already exists and is not empty")
        entries = list(output_dir.iterdir())
        unrelated = [
            entry for entry in entries if not entry.is_file() or not _is_generated_output(entry)
        ]
        if unrelated:
            raise FileExistsError(f"output directory {output_dir} contains non-generated files")
        for entry in entries:
            entry.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)
    for session in result.sessions:
        _write_jsonl(output_dir / f"session-{session.session_id:06d}.jsonl", session.rows)
    if result.unclassified:
        _write_jsonl(output_dir / "unclassified.jsonl", result.unclassified)
    if write_manifest:
        manifest = _manifest(
            result,
            source_path=source_path,
            window_size=window_size,
            match_threshold=match_threshold,
        )
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def split_export(
    source_path: Path,
    *,
    window_size: int,
    match_threshold: float,
) -> SplitResult:
    """Read a JSONL export and split valid rows into sessions."""
    read_result = read_jsonl_rows(source_path)
    split = assign_sessions(
        read_result.rows,
        window_size=window_size,
        match_threshold=match_threshold,
        invalid_lines=read_result.invalid_lines,
        total_lines=read_result.total_lines,
    )
    return SplitResult(
        sessions=split.sessions,
        unclassified=split.unclassified,
        invalid_lines=read_result.invalid_lines,
        total_lines=read_result.total_lines,
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Split tokenized API log JSONL rows into inferred prompt sessions."
    )
    parser.add_argument("input", type=Path, help="Path to tokenized prompt JSONL")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where session JSONL files will be written",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing non-empty output directory",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=200,
        help="Number of most-recent sessions to compare against (default: 200)",
    )
    parser.add_argument(
        "--match-threshold",
        type=float,
        default=0.80,
        help="LCP/max-length ratio required to continue a session (default: 0.80)",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="Do not write manifest.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the splitter CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    source_path = args.input
    if not source_path.is_file():
        print(f"ERROR: input file does not exist: {source_path}", file=sys.stderr)
        return 1
    if args.window_size <= 0:
        print("ERROR: --window-size must be > 0", file=sys.stderr)
        return 1
    if not 0.0 <= args.match_threshold <= 1.0:
        print("ERROR: --match-threshold must be between 0.0 and 1.0", file=sys.stderr)
        return 1
    try:
        output_dir = args.output_dir
        if not output_dir.is_absolute():
            output_dir = source_path.parent / output_dir
        result = split_export(
            source_path,
            window_size=args.window_size,
            match_threshold=args.match_threshold,
        )
        for invalid_line in result.invalid_lines:
            print(
                f"WARNING: skipped line {invalid_line.line_number}: {invalid_line.error}",
                file=sys.stderr,
            )
        write_split_result(
            result,
            output_dir=output_dir,
            source_path=source_path,
            overwrite=args.overwrite,
            write_manifest=not args.no_manifest,
            window_size=args.window_size,
            match_threshold=args.match_threshold,
        )
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Wrote {len(result.sessions)} sessions and {len(result.unclassified)} unclassified rows "
        f"to {output_dir}",
        file=sys.stderr,
    )
    _print_split_stats(
        result,
        match_threshold=args.match_threshold,
        window_size=args.window_size,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
