"""Split exported API log JSONL rows into inferred prompt-thread sessions."""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class InvalidLine:
    """Diagnostic for a JSONL line that could not become a row."""

    line_number: int
    error: str


@dataclass(frozen=True)
class NormalizedPrompt:
    """Comparable prompt representation used by the session assigner."""

    kind: str
    text: str
    tokens: frozenset[str]
    messages: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LogRow:
    """Valid API log row plus stable ordering metadata."""

    line_number: int
    row: dict[str, Any]
    timestamp: datetime | None
    prompt: NormalizedPrompt | None


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
    last_prompt: NormalizedPrompt | None = None
    last_timestamp: datetime | None = None
    updated_order: int = 0


@dataclass(frozen=True)
class SplitResult:
    """Complete result of splitting rows into sessions."""

    sessions: list[Session]
    unclassified: list[LogRow]
    invalid_lines: list[InvalidLine]
    total_lines: int


def _stable_json(value: Any) -> str:
    """Return deterministic JSON text for arbitrary JSON-compatible values."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _tokens(text: str) -> frozenset[str]:
    """Tokenize normalized text for deterministic overlap matching."""
    return frozenset(match.group(0).lower() for match in TOKEN_RE.finditer(text))


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an exported timestamp value when possible."""
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


def _message_content(message: dict[str, Any]) -> str:
    """Extract stable textual content from a chat message dictionary."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "\n".join(parts)
    fallback = {key: value for key, value in message.items() if key not in {"id", "timestamp"}}
    return _stable_json(fallback) if fallback else ""


def _messages_from_list(value: list[Any]) -> tuple[tuple[str, str], ...] | None:
    """Normalize a list of chat message dictionaries."""
    if not value:
        return None
    messages: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        role = str(item.get("role", ""))
        content = _message_content(item)
        if role or content:
            messages.append((role, content))
    return tuple(messages) if messages else None


def normalize_prompt(prompt: Any) -> NormalizedPrompt | None:
    """Convert a prompt value into a comparable prompt signature."""
    if prompt is None:
        return None
    candidate = prompt
    if isinstance(prompt, str):
        stripped = prompt.strip()
        if not stripped:
            return None
        candidate = stripped
        if stripped[0] in "[{":
            try:
                candidate = json.loads(stripped)
            except json.JSONDecodeError:
                candidate = stripped
    if isinstance(candidate, list):
        if not candidate:
            return None
        messages = _messages_from_list(candidate)
        if messages is not None:
            text = "\n".join(f"{role}: {content}" for role, content in messages)
            return NormalizedPrompt(
                kind="messages",
                text=text,
                tokens=_tokens(text),
                messages=messages,
            )
        text = _stable_json(candidate)
    elif isinstance(candidate, str):
        text = candidate.strip()
    else:
        text = _stable_json(candidate)
    if not text:
        return None
    return NormalizedPrompt(kind="text", text=text, tokens=_tokens(text))


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
                    timestamp=_parse_timestamp(value.get("timestamp")),
                    prompt=normalize_prompt(value.get("prompt")),
                )
            )
    return RowReadResult(rows=rows, invalid_lines=invalid_lines, total_lines=total_lines)


def _is_prefix(previous: tuple[tuple[str, str], ...], current: tuple[tuple[str, str], ...]) -> bool:
    """Return True when previous messages are a prefix of current messages."""
    return bool(previous) and len(current) >= len(previous) and current[: len(previous)] == previous


def _token_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """Return normalized token overlap from 0.0 to 1.0."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _within_gap(current: datetime | None, previous: datetime | None, max_gap_minutes: int) -> bool:
    """Return True when timestamps are missing or within the weak-match gap."""
    if current is None or previous is None:
        return True
    gap_seconds = abs((current - previous).total_seconds())
    return gap_seconds <= max_gap_minutes * 60


def _candidate_score(
    current: LogRow,
    session: Session,
    *,
    max_gap_minutes: int,
    overlap_threshold: float,
) -> float:
    """Score whether current row continues a session."""
    if current.prompt is None or session.last_prompt is None:
        return 0.0
    if _is_prefix(session.last_prompt.messages, current.prompt.messages):
        return 2.0
    if not _within_gap(current.timestamp, session.last_timestamp, max_gap_minutes):
        return 0.0
    overlap = _token_overlap(current.prompt.tokens, session.last_prompt.tokens)
    return overlap if overlap >= overlap_threshold else 0.0


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


def _select_candidate_session(
    current: LogRow,
    sessions: list[Session],
    *,
    max_gap_minutes: int,
    overlap_threshold: float,
) -> Session | None:
    """Return the unambiguous best matching session, if one exists."""
    scored = [
        (
            _candidate_score(
                current,
                session,
                max_gap_minutes=max_gap_minutes,
                overlap_threshold=overlap_threshold,
            ),
            session.updated_order,
            session,
        )
        for session in sessions
    ]
    scored = [item for item in scored if item[0] > 0.0]
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1], item[2].session_id), reverse=True)
    best_score, best_updated_order, best_session = scored[0]
    tied = [item for item in scored if item[0] == best_score and item[1] == best_updated_order]
    return best_session if len(tied) == 1 else None


def assign_sessions(
    rows: list[LogRow],
    *,
    max_gap_minutes: int,
    overlap_threshold: float,
    invalid_lines: list[InvalidLine] | None = None,
    total_lines: int | None = None,
) -> SplitResult:
    """Assign prompt-bearing rows into inferred sessions."""
    sessions: list[Session] = []
    unclassified: list[LogRow] = []
    update_order = 0
    for row in _sort_rows(rows):
        if row.prompt is None:
            unclassified.append(row)
            continue
        session = _select_candidate_session(
            row,
            sessions,
            max_gap_minutes=max_gap_minutes,
            overlap_threshold=overlap_threshold,
        )
        if session is None:
            session = Session(session_id=len(sessions) + 1)
            sessions.append(session)
        update_order += 1
        session.rows.append(row)
        session.last_prompt = row.prompt
        session.last_timestamp = row.timestamp
        session.updated_order = update_order
    return SplitResult(
        sessions=sessions,
        unclassified=unclassified,
        invalid_lines=invalid_lines or [],
        total_lines=total_lines if total_lines is not None else len(rows),
    )


def _write_jsonl(path: Path, rows: list[LogRow]) -> None:
    """Write original row objects to JSONL without mutating them."""
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row.row, ensure_ascii=False, default=str))
            handle.write("\n")


def _iso(value: datetime | None) -> str | None:
    """Return ISO text for a datetime or None."""
    return value.isoformat() if value is not None else None


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
    max_gap_minutes: int,
    overlap_threshold: float,
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
            "max_gap_minutes": max_gap_minutes,
            "overlap_threshold": overlap_threshold,
        },
    }


def write_split_result(
    result: SplitResult,
    *,
    output_dir: Path,
    source_path: Path,
    overwrite: bool,
    write_manifest: bool,
    max_gap_minutes: int,
    overlap_threshold: float,
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
            max_gap_minutes=max_gap_minutes,
            overlap_threshold=overlap_threshold,
        )
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def split_export(
    source_path: Path,
    *,
    max_gap_minutes: int,
    overlap_threshold: float,
) -> SplitResult:
    """Read a JSONL export and split valid rows into sessions."""
    read_result = read_jsonl_rows(source_path)
    split = assign_sessions(
        read_result.rows,
        max_gap_minutes=max_gap_minutes,
        overlap_threshold=overlap_threshold,
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
        description="Split an API log JSONL export into inferred prompt sessions."
    )
    parser.add_argument("input", type=Path, help="Path to api_logs_export.jsonl")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where session JSONL files will be written",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing non-empty output directory",
    )
    parser.add_argument(
        "--max-gap-minutes",
        type=int,
        default=120,
        help="Maximum gap for weak text-overlap matches (default: 120)",
    )
    parser.add_argument(
        "--overlap-threshold",
        type=float,
        default=0.65,
        help="Token-overlap threshold for weak matches (default: 0.65)",
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
    if args.max_gap_minutes < 0:
        print("ERROR: --max-gap-minutes must be >= 0", file=sys.stderr)
        return 1
    if not 0.0 <= args.overlap_threshold <= 1.0:
        print("ERROR: --overlap-threshold must be between 0.0 and 1.0", file=sys.stderr)
        return 1
    try:
        result = split_export(
            source_path,
            max_gap_minutes=args.max_gap_minutes,
            overlap_threshold=args.overlap_threshold,
        )
        for invalid_line in result.invalid_lines:
            print(
                f"WARNING: skipped line {invalid_line.line_number}: {invalid_line.error}",
                file=sys.stderr,
            )
        write_split_result(
            result,
            output_dir=args.output_dir,
            source_path=source_path,
            overwrite=args.overwrite,
            write_manifest=not args.no_manifest,
            max_gap_minutes=args.max_gap_minutes,
            overlap_threshold=args.overlap_threshold,
        )
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Wrote {len(result.sessions)} sessions and {len(result.unclassified)} unclassified rows "
        f"to {args.output_dir}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
