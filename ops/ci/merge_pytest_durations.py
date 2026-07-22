#!/usr/bin/env python3
"""Merge disjoint pytest shard duration manifests into one candidate manifest."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence


class MergeError(ValueError):
    """Raised when shard duration manifests cannot be merged safely."""


def _normalize_path(raw_path: str) -> str:
    path = PurePosixPath(raw_path.replace("\\", "/"))
    if path.is_absolute() or PureWindowsPath(raw_path).is_absolute() or ".." in path.parts:
        raise MergeError(f"duration path must stay within the repository: {raw_path!r}")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise MergeError("duration path must not be empty")
    return normalized


def read_duration_manifest(path: Path) -> dict[str, float]:
    """Read one version-1 duration manifest with strict validation."""
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeError(f"cannot read duration manifest {path}: {exc}") from exc

    version = payload.get("version") if isinstance(payload, dict) else None
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise MergeError(f"duration manifest {path} must use version 1")
    raw_durations = payload.get("durations")
    if not isinstance(raw_durations, dict):
        raise MergeError(f"duration manifest {path} must contain a durations object")

    durations: dict[str, float] = {}
    for raw_test_path, raw_duration in raw_durations.items():
        if (
            not isinstance(raw_test_path, str)
            or isinstance(raw_duration, bool)
            or not isinstance(raw_duration, (int, float))
        ):
            raise MergeError(f"duration manifest {path} contains an invalid entry")
        duration = float(raw_duration)
        if not math.isfinite(duration) or duration <= 0:
            raise MergeError(
                f"duration manifest {path} has a non-positive value for {raw_test_path!r}"
            )
        test_path = _normalize_path(raw_test_path)
        if test_path in durations:
            raise MergeError(f"duration manifest {path} normalizes duplicate path {test_path!r}")
        durations[test_path] = duration
    return durations


def merge_duration_files(paths: Sequence[Path]) -> dict[str, Any]:
    """Merge shard files while rejecting missing, empty, or overlapping input."""
    if not paths:
        raise MergeError("at least one duration manifest is required")

    merged: dict[str, float] = {}
    for path in sorted(paths):
        shard = read_duration_manifest(path)
        duplicates = sorted(set(merged).intersection(shard))
        if duplicates:
            raise MergeError(f"duration manifests overlap on test file(s): {', '.join(duplicates)}")
        merged.update(shard)

    if not merged:
        raise MergeError("duration manifests contain no test file timings")
    return {
        "version": 1,
        "durations": {path: round(merged[path], 6) for path in sorted(merged)},
    }


def write_duration_manifest_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a duration manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(OSError):
            temporary_path.unlink()
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="shard duration JSON files")
    parser.add_argument("--output", type=Path, required=True, help="merged candidate JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the duration manifest merger."""
    args = _build_parser().parse_args(argv)
    try:
        write_duration_manifest_atomic(args.output, merge_duration_files(args.inputs))
        return 0
    except (OSError, MergeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
