"""Temporary parity guard for the public documentation directory migration."""

from __future__ import annotations

import subprocess
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


def test_transitional_public_documentation_trees_match() -> None:
    """Keep both Pages build roots identical until the legacy root is retired."""
    legacy_files = _version_controlled_files(LEGACY_DOCS_ROOT)
    distribution_files = _version_controlled_files(DISTRIBUTION_DOCS_ROOT)

    assert legacy_files.keys() == distribution_files.keys()
    for relative_path in legacy_files:
        assert (
            legacy_files[relative_path].read_bytes()
            == distribution_files[relative_path].read_bytes()
        ), f"documentation trees differ at {relative_path}"
