#!/usr/bin/env python3
"""Classify which CI checks and application images a change set touches.

The classifier is deliberately conservative. Any file that does not match a
known narrow rule -- and any failure to compute the diff -- forces ``full`` so
CI never skips a check it should have run. See the CI/CD design doc, section
"Change Classifier", for the rule table and rationale.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Boolean workflow outputs, in a stable order.
CATEGORIES = (
    "backend",
    "frontend",
    "oncall",
    "docker_shared",
    "python_tests",
    "docs",
    "tutorial_e2e",
    "security_only",
    "full",
)

# Docker images are emitted in this order so the matrix is deterministic.
DOCKER_IMAGES = ("frontend", "backend", "oncall")

# Files whose blast radius is broad enough to force a full run. The repo-root
# README is included because it is a COPY input to the backend/oncall images.
FULL_FILES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "Makefile",
    }
)
FULL_PREFIXES = (
    ".github/workflows/",
    "config/",
    "distributions/",
)
# The one distributions/ subtree that must NOT force a full run: an overlay's
# doc-site corpus is site content, read only by the offline ingest CLI, never by
# the running application. Matched for any overlay rather than one by name —
# the rule is about what the directory is, and a second distribution would
# otherwise have forced a full run for a css edit.
_OVERLAY_DOCS = re.compile(r"^distributions/[^/]+/content/docs/")

FRONTEND_PREFIX = "apps/frontend/"
BACKEND_SOURCE_PREFIX = "apps/backend/"
BACKEND_TEST_PREFIX = "tests/"
# The other distributions/ subtree that must not force a full run. A real
# overlay does, because changing a deployment's registry or thresholds can
# affect anything; the runnable example is a teaching artifact whose only
# consumer is the backend Docker cell that executes its documented smoke
# contract. Kept before the Markdown-as-docs rule too, so editing its README
# cannot bypass the very CI path it documents.
BACKEND_EXAMPLE_PREFIX = "distributions/example/"
# The router tutorial states the example's commands and pins its model id,
# sentinel and published port, and tests assert all three. Classifying it as
# plain docs would let the page most likely to be read rot on a docs-only
# change. Named file rather than a prefix: only this page makes claims CI can
# check, and it still gates the Sphinx build as ordinary docs/developer/ source.
BACKEND_TUTORIAL_FILES = frozenset({"docs/developer/router-tutorial.md"})
# The full runnable tutorial exercises the example's orchestration plus the
# complete backend and frontend applications. Their internal imports are not a
# stable public boundary: a shared UI primitive, analytics helper, or logging
# task can break signup, Admin, completion history, or Playground even when its
# path does not name that feature. Select the E2E for either whole application
# tree rather than maintaining another incomplete dependency graph here.
TUTORIAL_E2E_FILES = frozenset(
    {
        ".dockerignore",
        "Makefile",
        "deploy/docker/docker-compose.yml",
        "deploy/docker/Dockerfile.backend",
        "deploy/docker/Dockerfile.backend.dockerignore",
        "deploy/docker/Dockerfile.frontend",
        "deploy/docker/Dockerfile.frontend.dockerignore",
        "docs/developer/router-tutorial.md",
    }
)
TUTORIAL_E2E_PREFIXES = (
    BACKEND_EXAMPLE_PREFIX,
    "apps/backend/",
    "apps/frontend/",
)
# Dockerfile.oncall COPYs the entire apps/backend/serving tree, so any serving
# change -- not just serving/oncall -- is baked into the on-call image and must
# rebuild it. Keep this in sync with that Dockerfile's COPY scope.
ONCALL_PREFIX = "apps/backend/serving/"
DOCKER_SHARED_FILES = frozenset(
    {
        ".dockerignore",
        "deploy/docker/docker-compose.yml",
    }
)
DOCKER_IMAGE_FILES = {
    "deploy/docker/Dockerfile.frontend": "frontend",
    "deploy/docker/Dockerfile.frontend.dockerignore": "frontend",
    "deploy/docker/Dockerfile.backend": "backend",
    "deploy/docker/Dockerfile.backend.dockerignore": "backend",
    "deploy/docker/Dockerfile.oncall": "oncall",
    "deploy/docker/Dockerfile.oncall.dockerignore": "oncall",
}

# Documentation never triggers application checks. Any markdown outside the
# repo-root README counts as docs regardless of directory.
DOCS_FILES = frozenset({"LICENSE"})
DOCS_PREFIXES = ("docs/",)
# Sphinx source tree for the internal doc site (internaldoc.freeinference.org).
# Only this subtree feeds `sphinx-build docs/developer`, so it -- not docs in
# general -- gates the docs build. The toctree is self-contained and no page
# uses autodoc, so nothing outside this prefix can break that build.
SPHINX_SOURCE_PREFIX = "docs/developer/"


@dataclass
class Classification:
    """Result of classifying a change set into CI trigger booleans."""

    backend: bool = False
    frontend: bool = False
    oncall: bool = False
    docker_shared: bool = False
    python_tests: bool = False
    docs: bool = False
    tutorial_e2e: bool = False
    security_only: bool = False
    full: bool = False
    reason: str = ""
    matched: dict[str, list[str]] = field(default_factory=dict)
    docker_images: set[str] = field(default_factory=set)

    def as_outputs(self) -> dict[str, str]:
        """Return the GitHub-Actions string outputs for each category."""
        return {name: ("true" if getattr(self, name) else "false") for name in CATEGORIES}

    def docker_matrix(self) -> list[str]:
        """Return affected application images in stable matrix order."""
        if self.full or self.docker_shared:
            return list(DOCKER_IMAGES)
        return [image for image in DOCKER_IMAGES if image in self.docker_images]

    def as_workflow_outputs(self) -> dict[str, str]:
        """Return all GitHub Actions outputs, including the Docker matrix."""
        return {
            **self.as_outputs(),
            "docker_matrix": json.dumps(self.docker_matrix(), separators=(",", ":")),
        }


def _normalize(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip().rstrip("/")


def _is_full(path: str) -> bool:
    if _OVERLAY_DOCS.match(path) or path.startswith(BACKEND_EXAMPLE_PREFIX):
        return False
    return path in FULL_FILES or any(path.startswith(prefix) for prefix in FULL_PREFIXES)


def _is_docs(path: str) -> bool:
    # An overlay's doc-site source counts as documentation too: its
    # non-markdown files (Makefile, css, conf.py) are still documentation, not
    # application inputs.
    if (
        path in DOCS_FILES
        or any(path.startswith(prefix) for prefix in DOCS_PREFIXES)
        or _OVERLAY_DOCS.match(path)
    ):
        return True
    # Any markdown other than the repo-root README (handled by FULL_FILES).
    return path.endswith(".md")


def _is_tutorial_e2e(path: str) -> bool:
    if (
        path.endswith(".md")
        and path not in TUTORIAL_E2E_FILES
        and not path.startswith(BACKEND_EXAMPLE_PREFIX)
    ):
        return False
    return path in TUTORIAL_E2E_FILES or any(
        path.startswith(prefix) for prefix in TUTORIAL_E2E_PREFIXES
    )


def classify(files: Sequence[str] | None) -> Classification:
    """Classify a diff into trigger booleans; ``None`` forces a full run."""
    if files is None:
        return Classification(
            full=True,
            python_tests=True,
            reason="diff unavailable; forcing full run",
        )

    normalized = sorted({_normalize(path) for path in files if path.strip()})
    if not normalized:
        return Classification(full=True, python_tests=True, reason="empty diff; forcing full run")

    result = Classification()
    matched: dict[str, list[str]] = {}
    unknown: list[str] = []

    def hit(category: str, path: str) -> None:
        matched.setdefault(category, []).append(path)

    for path in normalized:
        # The E2E contract is orthogonal to the ordinary application buckets.
        # Record it before branches that `continue`, including full triggers
        # such as Makefile and the runnable tutorial's Markdown.
        if _is_tutorial_e2e(path):
            result.tutorial_e2e = True
            hit("tutorial_e2e", path)
        # 1. Full triggers win outright (broadest blast radius / build inputs).
        if _is_full(path):
            result.full = True
            result.python_tests = True
            hit("full", path)
            continue
        # Runnable examples are backend image inputs and CI acceptance inputs,
        # including their Markdown instructions.
        if path.startswith(BACKEND_EXAMPLE_PREFIX) or path in BACKEND_TUTORIAL_FILES:
            result.backend = True
            result.python_tests = True
            result.docker_images.add("backend")
            if path.startswith(SPHINX_SOURCE_PREFIX):
                result.docs = True
            hit("backend", path)
            continue
        # 2. Documentation never triggers application checks, but the Sphinx
        #    source tree gates the docs build.
        if _is_docs(path):
            if path.startswith(SPHINX_SOURCE_PREFIX):
                result.docs = True
                hit("docs", path)
            else:
                hit("docs_other", path)
            continue
        # 3. Narrow buckets (a path may hit more than one, e.g. oncall+backend).
        recognized = False
        if path.startswith(FRONTEND_PREFIX):
            result.frontend = True
            result.docker_images.add("frontend")
            hit("frontend", path)
            recognized = True
        if path.startswith(BACKEND_SOURCE_PREFIX) or path.startswith(BACKEND_TEST_PREFIX):
            result.backend = True
            result.python_tests = True
            hit("backend", path)
            recognized = True
            # Tests exercise backend code but are not COPY inputs to an image.
            if path.startswith(BACKEND_SOURCE_PREFIX):
                result.docker_images.add("backend")
        if path.startswith(ONCALL_PREFIX):
            result.oncall = True
            hit("oncall", path)
            recognized = True
            result.docker_images.add("oncall")
        if path in DOCKER_SHARED_FILES:
            result.docker_shared = True
            result.python_tests = True
            hit("docker_shared", path)
            recognized = True
        if image := DOCKER_IMAGE_FILES.get(path):
            result.docker_images.add(image)
            result.python_tests = True
            hit(image, path)
            recognized = True
            if image == "frontend":
                result.frontend = True
            elif image == "backend":
                result.backend = True
            else:
                result.oncall = True
        # 4. Unknown path -> conservative full run.
        if not recognized:
            result.full = True
            result.python_tests = True
            unknown.append(path)
            hit("full", path)

    narrow = (
        result.frontend
        or result.backend
        or result.oncall
        or result.docker_shared
        or result.python_tests
        or result.tutorial_e2e
    )
    # `docs` is deliberately absent from `narrow`: it gates the docs build, not
    # an application check, so a docs-only change stays security_only.
    if not result.full and not narrow:
        # Everything was documentation: only the docs build (when the Sphinx
        # source changed), the security scan, and the gate are needed.
        result.security_only = True

    if unknown:
        result.reason = f"unknown paths force full: {', '.join(sorted(unknown))}"
    elif result.full:
        result.reason = "matched full triggers"
    elif result.security_only:
        suffix = " + docs build" if result.docs else ""
        result.reason = f"docs-only change; only security + gate{suffix} needed"
    else:
        result.reason = "matched narrow rules"

    result.matched = matched
    return result


def _run_git(repo_root: Path, args: Sequence[str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout


def _commit_exists(repo_root: Path, sha: str | None) -> bool:
    if not sha or set(sha) <= {"0"}:
        return False
    code, _ = _run_git(repo_root, ["cat-file", "-e", f"{sha}^{{commit}}"])
    return code == 0


def compute_changed_files(
    repo_root: Path,
    event_name: str,
    pr_base: str | None,
    pr_head: str | None,
    push_before: str | None,
    push_sha: str | None,
) -> list[str] | None:
    """Return changed files for the event, or ``None`` when it cannot be known.

    PRs use three-dot ``base...head`` (changes the branch introduced since it
    diverged from base); pushes use two-dot ``before..sha`` (endpoint diff) so a
    force / non-fast-forward push that drops files on the old tip is still seen.

    ``--no-renames`` is intentional: a rename then surfaces as delete(old) +
    add(new), so both the old and new path are considered by the classifier.
    """
    if event_name == "pull_request":
        base, head = pr_base, pr_head
        if not _commit_exists(repo_root, base) or not _commit_exists(repo_root, head):
            return None
        diff_range = f"{base}...{head}"
    elif event_name == "push":
        if not _commit_exists(repo_root, push_before) or not _commit_exists(repo_root, push_sha):
            # First push / branch creation (zero base) or unreachable SHA.
            return None
        # Two-dot endpoint comparison (NOT three-dot). A non-fast-forward /
        # force push can drop commits that only existed on the old tip;
        # three-dot (merge-base..sha) would miss those removed files and could,
        # e.g., misclassify a diverged push as frontend-only.
        diff_range = f"{push_before}..{push_sha}"
    else:
        # workflow_dispatch and anything else: no reliable base -> full.
        return None

    code, out = _run_git(repo_root, ["diff", "--name-only", "--no-renames", diff_range])
    if code != 0:
        return None
    return [line for line in out.splitlines() if line.strip()]


def _write_outputs(result: Classification, output_path: str | None) -> None:
    if not output_path:
        return
    lines = [f"{name}={value}" for name, value in result.as_workflow_outputs().items()]
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _write_summary(
    result: Classification, summary_path: str | None, file_count: int | None
) -> None:
    if not summary_path:
        return
    lines = [
        "## Active change classification",
        "",
        f"- **reason:** {result.reason}",
        f"- **changed files:** {file_count if file_count is not None else 'unavailable'}",
        "",
        "| output | value |",
        "|---|---|",
    ]
    lines += [f"| `{name}` | {value} |" for name, value in result.as_outputs().items()]
    lines.append(f"| `docker_matrix` | `{json.dumps(result.docker_matrix())}` |")
    # `docs_other` is documentation with no CI consequence at all; the `docs`
    # bucket does gate the docs build, so it stays in the summary.
    narrow_matches = {k: v for k, v in result.matched.items() if k != "docs_other"}
    if narrow_matches:
        lines += ["", "<details><summary>matched files</summary>", ""]
        for category in sorted(narrow_matches):
            shown = narrow_matches[category][:20]
            more = len(narrow_matches[category]) - len(shown)
            suffix = f" (+{more} more)" if more > 0 else ""
            lines.append(f"- **{category}**: {', '.join(f'`{p}`' for p in shown)}{suffix}")
        lines += ["", "</details>"]
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", default="", help="github.event_name")
    parser.add_argument("--pr-base", default="", help="pull_request base SHA")
    parser.add_argument("--pr-head", default="", help="pull_request head SHA")
    parser.add_argument("--push-before", default="", help="push before SHA")
    parser.add_argument("--push-sha", default="", help="push head SHA")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository root (default: inferred from this script)",
    )
    parser.add_argument(
        "--changed-files-file",
        type=Path,
        help="read newline-separated changed files instead of running git (testing/debug)",
    )
    parser.add_argument("--github-output", help="append name=value outputs here (GITHUB_OUTPUT)")
    parser.add_argument("--summary", help="append a markdown summary here (GITHUB_STEP_SUMMARY)")
    parser.add_argument("--print-json", action="store_true", help="print the result as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the classifier. Diff failures conservatively classify as ``full``."""
    args = _build_parser().parse_args(argv)

    files: list[str] | None
    if args.changed_files_file is not None:
        try:
            text = args.changed_files_file.read_text(encoding="utf-8")
            files = [line for line in text.splitlines() if line.strip()]
        except OSError:
            files = None
    else:
        try:
            files = compute_changed_files(
                args.repo_root.resolve(),
                args.event_name,
                args.pr_base,
                args.pr_head,
                args.push_before,
                args.push_sha,
            )
        except Exception as exc:  # an unavailable diff must never cause an unsafe skip
            print(f"warning: change classification failed ({exc}); forcing full", file=sys.stderr)
            files = None

    result = classify(files)
    _write_outputs(result, args.github_output)
    _write_summary(result, args.summary, None if files is None else len(files))

    payload = {**result.as_workflow_outputs(), "reason": result.reason}
    if args.print_json:
        print(json.dumps(payload))
    else:
        print(f"classification: {payload}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
