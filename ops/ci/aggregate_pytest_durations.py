#!/usr/bin/env python3
"""Aggregate successful CI-run pytest timings into a commit-ready candidate."""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if __package__:
    from .merge_pytest_durations import (
        MergeError,
        read_duration_manifest,
        write_duration_manifest_atomic,
    )
    from .partition_pytest_files import PartitionError, discover_test_files
else:
    from merge_pytest_durations import (  # type: ignore[import-not-found]
        MergeError,
        read_duration_manifest,
        write_duration_manifest_atomic,
    )
    from partition_pytest_files import (  # type: ignore[import-not-found]
        PartitionError,
        discover_test_files,
    )

if TYPE_CHECKING:
    from collections.abc import Sequence


class AggregateError(ValueError):
    """Raised when CI-run duration manifests cannot produce a safe candidate."""


def aggregate_duration_files(
    paths: Sequence[Path],
    current_test_files: Sequence[str],
) -> dict[str, Any]:
    """Take each current test file's median timing across successful CI runs."""
    if not paths:
        raise AggregateError("at least one CI-run duration manifest is required")
    if not current_test_files:
        raise AggregateError("the repository contains no current pytest files")

    current = set(current_test_files)
    samples: defaultdict[str, list[float]] = defaultdict(list)
    for path in sorted(paths):
        run_durations = read_duration_manifest(path)
        if not run_durations:
            raise AggregateError(f"duration manifest {path} contains no test file timings")
        for test_path, duration in run_durations.items():
            if test_path in current:
                samples[test_path].append(duration)

    if not samples:
        raise AggregateError("duration manifests contain no timings for current pytest files")

    durations = {path: round(statistics.median(samples[path]), 6) for path in sorted(samples)}
    return {"version": 1, "durations": durations}


def _resolve_from_repo(repo_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="successful CI-run timing JSON files")
    parser.add_argument("--output", type=Path, required=True, help="commit-ready candidate JSON")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the cross-run duration aggregator."""
    args = _build_parser().parse_args(argv)
    try:
        repo_root = args.repo_root.resolve()
        pyproject_path = _resolve_from_repo(repo_root, args.pyproject)
        current_test_files = discover_test_files(repo_root, pyproject_path)
        candidate = aggregate_duration_files(args.inputs, current_test_files)
        write_duration_manifest_atomic(args.output, candidate)
        print(
            f"Wrote timings for {len(candidate['durations'])} current pytest files "
            f"from {len(args.inputs)} successful CI runs.",
            file=sys.stderr,
        )
        return 0
    except (AggregateError, MergeError, OSError, PartitionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
