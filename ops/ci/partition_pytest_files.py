#!/usr/bin/env python3
"""Partition pytest files into deterministic, duration-balanced shards."""

from __future__ import annotations

import argparse
import ast
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_DURATION_SECONDS = 1.0
DEFAULT_DURATION_PATH = Path("ops/ci/pytest-file-durations.json")
REQUIRED_TEST_ROOTS = frozenset({"tests", "distributions"})


class PartitionError(ValueError):
    """Raised when test discovery or shard validation cannot proceed safely."""


@dataclass(frozen=True)
class Shard:
    """One deterministic collection of pytest files and its estimated duration."""

    files: tuple[str, ...]
    estimated_seconds: float


def _normalize_repo_path(raw_path: str) -> str:
    path = PurePosixPath(raw_path.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise PartitionError(f"path must stay within the repository: {raw_path!r}")

    normalized = path.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in {"", "."}:
        raise PartitionError(f"path must not be empty: {raw_path!r}")
    return normalized.rstrip("/")


def read_pytest_testpaths(pyproject_path: Path) -> tuple[str, ...]:
    """Read pytest testpaths from pyproject.toml without a third-party TOML parser."""
    try:
        text = pyproject_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PartitionError(f"cannot read pytest configuration {pyproject_path}: {exc}") from exc

    section_lines: list[str] = []
    in_pytest_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_pytest_section:
                break
            in_pytest_section = stripped == "[tool.pytest.ini_options]"
            continue
        if in_pytest_section:
            section_lines.append(line)

    if not section_lines:
        raise PartitionError(f"[tool.pytest.ini_options] is missing from {pyproject_path}")

    value_lines: list[str] = []
    collecting = False
    bracket_depth = 0
    for line in section_lines:
        if not collecting:
            key, separator, value = line.partition("=")
            if not separator or key.strip() != "testpaths":
                continue
            collecting = True
            value_lines.append(value.strip())
            bracket_depth = value.count("[") - value.count("]")
        else:
            value_lines.append(line.strip())
            bracket_depth += line.count("[") - line.count("]")

        if collecting and bracket_depth <= 0:
            break

    if not value_lines:
        raise PartitionError(f"pytest testpaths is missing from {pyproject_path}")

    try:
        raw_testpaths = ast.literal_eval("\n".join(value_lines))
    except (SyntaxError, ValueError) as exc:
        raise PartitionError(f"cannot parse pytest testpaths in {pyproject_path}: {exc}") from exc

    if not isinstance(raw_testpaths, (list, tuple)) or not raw_testpaths:
        raise PartitionError("pytest testpaths must be a non-empty array")
    if not all(isinstance(path, str) for path in raw_testpaths):
        raise PartitionError("pytest testpaths entries must all be strings")

    testpaths = tuple(_normalize_repo_path(path) for path in raw_testpaths)
    missing_required = sorted(REQUIRED_TEST_ROOTS.difference(testpaths))
    if missing_required:
        joined = ", ".join(missing_required)
        raise PartitionError(f"pytest testpaths must include repository roots: {joined}")
    return testpaths


def discover_test_files(repo_root: Path, pyproject_path: Path) -> tuple[str, ...]:
    """Discover every test_*.py file below the configured pytest test roots."""
    repo_root = repo_root.resolve()
    testpaths = read_pytest_testpaths(pyproject_path)
    discovered: set[str] = set()

    for relative_root in testpaths:
        root = repo_root / relative_root
        if not root.exists():
            raise PartitionError(f"configured pytest test root does not exist: {relative_root}")

        candidates = [root] if root.is_file() else root.rglob("test_*.py")
        for candidate in candidates:
            if (
                candidate.is_file()
                and candidate.name.startswith("test_")
                and candidate.suffix == ".py"
            ):
                try:
                    relative_path = candidate.resolve().relative_to(repo_root).as_posix()
                except ValueError as exc:
                    raise PartitionError(
                        f"test file resolves outside the repository: {candidate}"
                    ) from exc
                discovered.add(relative_path)

    if not discovered:
        raise PartitionError("pytest testpaths contain no test_*.py files")
    return tuple(sorted(discovered))


def _coerce_duration_map(payload: Any) -> dict[str, float]:
    if not isinstance(payload, dict):
        raise PartitionError("duration history must be a JSON object")

    if "durations" in payload:
        version = payload.get("version", payload.get("schema_version", 1))
        if version != 1:
            raise PartitionError(f"unsupported duration history version: {version!r}")
        raw_durations = payload["durations"]
    else:
        raw_durations = payload

    if not isinstance(raw_durations, dict):
        raise PartitionError("duration history 'durations' must be a JSON object")

    durations: dict[str, float] = {}
    for raw_path, raw_duration in raw_durations.items():
        if not isinstance(raw_path, str) or isinstance(raw_duration, bool):
            continue
        try:
            duration = float(raw_duration)
            path = _normalize_repo_path(raw_path)
        except (PartitionError, TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            durations[path] = duration
    return durations


def load_duration_history(duration_path: Path) -> tuple[dict[str, float], str | None]:
    """Load optional timing history, falling back to no history on any file error."""
    try:
        payload = json.loads(duration_path.read_text(encoding="utf-8"))
        return _coerce_duration_map(payload), None
    except (OSError, json.JSONDecodeError, PartitionError) as exc:
        warning = f"cannot use duration history {duration_path}: {exc}; using uniform weights"
        return {}, warning


def _p75(values: Sequence[float]) -> float:
    """Return the nearest-rank 75th percentile, which is conservative for small samples."""
    if not values:
        raise PartitionError("cannot calculate P75 from an empty sequence")
    ordered = sorted(values)
    return ordered[math.ceil(0.75 * len(ordered)) - 1]


def build_file_weights(
    test_files: Sequence[str],
    durations: dict[str, float],
    default_duration: float = DEFAULT_DURATION_SECONDS,
) -> tuple[dict[str, float], float]:
    """Assign known durations and a P75/default weight to files without history."""
    if not math.isfinite(default_duration) or default_duration <= 0:
        raise PartitionError("default duration must be a finite positive number")

    known = [durations[path] for path in test_files if path in durations]
    unknown_weight = _p75(known) if known else default_duration
    weights = {path: durations.get(path, unknown_weight) for path in test_files}
    return weights, unknown_weight


def validate_partitions(test_files: Sequence[str], shards: Sequence[Shard]) -> None:
    """Ensure every discovered test file appears in exactly one shard."""
    expected = set(test_files)
    assigned = [path for shard in shards for path in shard.files]
    counts = Counter(assigned)
    duplicates = sorted(path for path, count in counts.items() if count > 1)
    missing = sorted(expected.difference(counts))
    unexpected = sorted(set(counts).difference(expected))

    problems: list[str] = []
    if duplicates:
        problems.append(f"duplicate files: {', '.join(duplicates)}")
    if missing:
        problems.append(f"missing files: {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected files: {', '.join(unexpected)}")
    if problems:
        raise PartitionError("invalid pytest partition: " + "; ".join(problems))


def partition_test_files(
    test_files: Sequence[str],
    shard_count: int,
    durations: dict[str, float] | None = None,
    default_duration: float = DEFAULT_DURATION_SECONDS,
) -> tuple[tuple[Shard, ...], float]:
    """Partition files with deterministic longest-processing-time-first greedy packing."""
    if shard_count <= 0:
        raise PartitionError("shard count must be positive")
    if not test_files:
        raise PartitionError("cannot partition an empty test file list")
    if shard_count > len(test_files):
        raise PartitionError(
            f"shard count ({shard_count}) exceeds discovered test file count ({len(test_files)})"
        )
    if len(set(test_files)) != len(test_files):
        raise PartitionError("discovered test file list contains duplicates")

    weights, unknown_weight = build_file_weights(
        test_files,
        durations or {},
        default_duration=default_duration,
    )
    shard_files: list[list[str]] = [[] for _ in range(shard_count)]
    shard_totals = [0.0] * shard_count

    for path in sorted(test_files, key=lambda item: (-weights[item], item)):
        shard_index = min(range(shard_count), key=lambda index: (shard_totals[index], index))
        shard_files[shard_index].append(path)
        shard_totals[shard_index] += weights[path]

    shards = tuple(
        Shard(files=tuple(sorted(files)), estimated_seconds=shard_totals[index])
        for index, files in enumerate(shard_files)
    )
    validate_partitions(test_files, shards)
    return shards, unknown_weight


def _resolve_from_repo(repo_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int, required=True, help="1-based shard index")
    parser.add_argument("--shard-count", type=int, required=True, help="total number of shards")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (default: inferred from this script)",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="pytest configuration path, relative to the repository root",
    )
    parser.add_argument(
        "--durations",
        "--durations-file",
        dest="durations",
        type=Path,
        default=DEFAULT_DURATION_PATH,
        help="optional duration JSON path, relative to the repository root",
    )
    parser.add_argument(
        "--default-duration",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
        help="weight used when no usable historical duration exists",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write one test path per line here (default: stdout)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line partitioner."""
    args = _build_parser().parse_args(argv)
    try:
        if args.shard_count <= 0:
            raise PartitionError("shard count must be positive")
        if not 1 <= args.shard_index <= args.shard_count:
            raise PartitionError(
                f"shard index must be between 1 and {args.shard_count}: {args.shard_index}"
            )

        repo_root = args.repo_root.resolve()
        pyproject_path = _resolve_from_repo(repo_root, args.pyproject)
        duration_path = _resolve_from_repo(repo_root, args.durations)
        test_files = discover_test_files(repo_root, pyproject_path)
        durations, warning = load_duration_history(duration_path)
        shards, unknown_weight = partition_test_files(
            test_files,
            args.shard_count,
            durations=durations,
            default_duration=args.default_duration,
        )
        selected = shards[args.shard_index - 1]

        contents = "\n".join(selected.files) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(contents, encoding="utf-8")
        else:
            sys.stdout.write(contents)

        if warning:
            print(f"warning: {warning}", file=sys.stderr)
        unknown_count = sum(path not in durations for path in test_files)
        print(
            f"Selected {len(selected.files)} of {len(test_files)} test files for shard "
            f"{args.shard_index}/{args.shard_count}; estimated {selected.estimated_seconds:.3f}s; "
            f"unknown files use {unknown_weight:.3f}s ({unknown_count} total)",
            file=sys.stderr,
        )
        return 0
    except (OSError, PartitionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
