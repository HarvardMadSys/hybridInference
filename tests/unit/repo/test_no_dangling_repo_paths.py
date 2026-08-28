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

"Executes" is broader than it first looks, and the first version of this test
got it wrong in the way that mattered most: it scanned workflows, shell scripts
and the Makefile, and skipped the systemd units — the one place that actually
spells out `__REPO_ROOT__/ops/...`, and the reason the ops/ move needs a guard
at all. Dockerfiles COPY repo paths and fail the build when one moves. And a
reference written as `${REPO_DIR}/deploy/systemd/...` was invisible to the
pattern, which is how installers name things.
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
        if n.startswith(".github/workflows/")
        or n.endswith((".sh", ".service"))
        or (n.startswith("deploy/docker/Dockerfile") and not n.endswith(".dockerignore"))
        or (n.startswith("deploy/docker/") and n.endswith((".yml", ".yaml")))
        or n == "Makefile"
    ]


# `${REPO_DIR}/deploy/...` and `__REPO_ROOT__/ops/...` are repo paths wearing a
# prefix. Dropping the prefix and the slash after it makes them visible; leaving
# them in place is how the first version of this test missed every systemd unit
# and every installer.
_ROOT_PREFIX = re.compile(r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|__[A-Z][A-Z0-9_]*__)/")

# `SERVICE_NAME="spark_idle_proxy"` — a literal a later line interpolates. Only
# assignments with no substitution of their own; anything computed stays a
# variable and the path it forms stays partly unresolvable.
_LITERAL_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=\"?([A-Za-z0-9_.\-/]+)\"?\s*(?:#.*)?$",
    re.MULTILINE,
)


def _literal_variables(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _LITERAL_ASSIGNMENT.finditer(text)}


def _strip_root_prefixes(line: str, variables: dict[str, str] | None = None) -> str:
    """Reduce a written path to the repo-relative form it names.

    Three shapes hide a repository path from a naive match, and every one of
    them appears in this tree: a root variable in front (`${REPO_DIR}/ops/...`,
    `__REPO_ROOT__/ops/...`), a variable standing in for part of the path
    (`${SERVICE_NAME}.service`), and a dot-relative prefix (`./apps/frontend`,
    `../../config`). The first two are substituted, the third normalised.
    """
    for name, value in (variables or {}).items():
        line = line.replace("${" + name + "}", value).replace("$" + name, value)
    line = _ROOT_PREFIX.sub("", line)
    # Dot-relative prefixes are dropped rather than resolved against the file's
    # own directory. Every occurrence in this tree sits at the depth where the
    # two agree (`../../config` from deploy/docker/), and erring here produces a
    # false positive rather than a miss, which is the direction to err in.
    line = re.sub(r"(?<![\w.])\.{1,2}/(?:\.\./)*", "", line)
    return line


def _is_dockerfile(name: str) -> bool:
    return name.startswith("deploy/docker/Dockerfile")


def _is_repo_relative_docker_line(line: str) -> bool:
    """Only COPY/ADD read from the build context, and not across stages."""
    head = line.strip().upper()
    if not head.startswith(("COPY ", "ADD ")):
        return False
    return "--FROM=" not in head


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
        variables = _literal_variables(text)
        for number, raw_line in enumerate(text.splitlines(), start=1):
            if raw_line.strip().startswith("#"):
                continue
            line = _strip_root_prefixes(raw_line, variables)
            if _is_dockerfile(name) and not _is_repo_relative_docker_line(line):
                # RUN operates inside the image: `pip install -e tests/x` after
                # `cd /vllm-workspace` names a container path, not this tree.
                # Only COPY/ADD sources come from the build context.
                continue
            for match in PATH_RE.finditer(line):
                path = match.group(1).rstrip(".,;:)\"'")
                if "*" in path:
                    continue
                if _is_templated(line, match):
                    # `${REPO_DIR}/deploy/systemd/${SERVICE_NAME}.service` names
                    # a file only at run time, but the directory it names is
                    # fixed — and moving that directory is exactly what breaks
                    # the installer. Check as much as is literal.
                    directory = path.rsplit("/", 1)[0] if "/" in path else path
                    if directory and not (REPO / directory).exists():
                        findings.append(f"{name}:{number} -> {directory}/ (from {path})")
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
    assert any(s.endswith(".service") for s in surfaces), (
        "the systemd units name __REPO_ROOT__/ops/... — the very paths the move has to update"
    )
    assert any(s.startswith("deploy/docker/Dockerfile") for s in surfaces), (
        "a Dockerfile COPY of a path that moved breaks the build"
    )
