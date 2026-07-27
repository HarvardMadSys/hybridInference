#!/usr/bin/env python3
"""Keep the temporary legacy and distribution documentation trees identical."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_DOCS_ROOT = REPO_ROOT / "docs/free_inference"
DISTRIBUTION_DOCS_ROOT = REPO_ROOT / "distributions/freeinference/content/docs"


def _version_controlled_files(root: Path) -> dict[Path, Path]:
    relative_root = root.relative_to(REPO_ROOT)
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            str(relative_root),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        path.relative_to(relative_root): REPO_ROOT / path
        for line in result.stdout.splitlines()
        if line
        for path in (Path(line),)
    }


def find_parity_errors() -> list[str]:
    """Return differences between the two tracked documentation trees."""
    legacy_files = _version_controlled_files(LEGACY_DOCS_ROOT)
    distribution_files = _version_controlled_files(DISTRIBUTION_DOCS_ROOT)
    errors = [
        *(
            f"missing from distribution tree: {path}"
            for path in legacy_files.keys() - distribution_files
        ),
        *(f"missing from legacy tree: {path}" for path in distribution_files.keys() - legacy_files),
    ]
    errors.extend(
        f"documentation trees differ at {path}"
        for path in legacy_files.keys() & distribution_files
        if legacy_files[path].read_bytes() != distribution_files[path].read_bytes()
    )
    return sorted(errors)


def main() -> int:
    """Check parity and print actionable errors for CI."""
    errors = find_parity_errors()
    if errors:
        print("public documentation transition parity check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("public documentation transition trees match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
