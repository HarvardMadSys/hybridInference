"""Anything that executes must name paths that exist.

The remaining half of the split moves `ops/`, `services/` and
`deploy/systemd/` into a distribution overlay. Unlike the config files — each
of which had a single env var to rewire, pinned by a test — those are named
directly by deploy scripts, CI workflows and the Makefile, in around ninety
places. And the deploy scripts do not run in CI, so a reference missed during
the move would surface on the server, mid-deploy, rather than on the pull
request.

This closes that gap ahead of the move: every repo-relative path written into
something that executes is resolved, and a miss fails here. It is deliberately
scoped to executable surfaces — prose in documentation can describe a layout
that no longer exists without breaking anything, while a shell script cannot.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

# Top-level directories worth resolving. A bare word like `build/` could mean
# anything; these are unambiguous repository paths.
TOP_LEVEL = (
    "ops",
    "config",
    "deploy",
    "apps",
    "services",
    "distributions",
    "tests",
    ".github",
)
PATH_RE = re.compile(
    r"(?<![\w/.-])((?:" + "|".join(re.escape(t) for t in TOP_LEVEL) + r")/[A-Za-z0-9_./-]+)"
)


def _executable_surfaces() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    names = [n for n in out.decode().split("\0") if n]
    return [
        n
        for n in names
        if n.startswith(".github/workflows/") or n.endswith(".sh") or n == "Makefile"
    ]


def _is_templated(line: str, match: re.Match[str]) -> bool:
    """True when the path is a prefix of something built at run time."""
    tail = line[match.end() : match.end() + 3]
    return tail.startswith((".$", "$", "{", "*")) or "$" in match.group(1)


def test_no_executable_file_names_a_path_that_is_gone() -> None:
    """A missed rename here breaks a deploy, not a test — unless this runs."""
    findings: list[str] = []

    for name in _executable_surfaces():
        try:
            text = (REPO / name).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            for match in PATH_RE.finditer(line):
                path = match.group(1).rstrip(".,;:)\"'")
                if "*" in path or _is_templated(line, match):
                    continue
                if not (REPO / path).exists():
                    findings.append(f"{name}:{number} -> {path}")

    assert not findings, (
        "these name repository paths that do not exist; if something moved, "
        f"the reference has to move with it: {findings}"
    )


def test_the_scan_actually_reaches_the_deploy_scripts() -> None:
    """A scan that silently covered nothing would pass forever."""
    surfaces = _executable_surfaces()
    assert "Makefile" in surfaces
    assert any(s.startswith(".github/workflows/") for s in surfaces)
    assert any("deploy" in s and s.endswith(".sh") for s in surfaces), (
        "the deploy scripts are the reason this test exists"
    )
