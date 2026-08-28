"""Inventory private deployment metadata in the repository that will be public.

This is a direct-tree migration guard, not a publication filter. The default
run fails on a finding outside an explicitly owned migration bucket. ``--strict``
also fails while any bucket still contains findings and is the visibility-flip
acceptance command.

    uv run python ops/admin/private_surface_sweep.py
    uv run python ops/admin/private_surface_sweep.py --strict
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

PATTERNS: dict[str, re.Pattern[str]] = {
    "personal mailbox": re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?:gmail|googlemail|outlook|hotmail|live|icloud|"
        r"me|yahoo|qq|163|126|foxmail)\.(?:com|me)\b",
        re.IGNORECASE,
    ),
    "personal home path": re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/[A-Za-z0-9._-]+"),
    "internal hostname": re.compile(
        r"\b(?:internal|staging-internal)\.[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        r"|\b(?:spark2|h200|holygpu\d*[a-z0-9]*)\b"
    ),
    "cluster path": re.compile(r"(?:/n)?/(?:net)?scratch/[A-Za-z0-9_./-]+"),
    "cloudflare identifier": re.compile(r'(?:account_id|database_id)\s*=\s*"[0-9a-f-]{32,}"'),
}

# These are migration work streams, not publication exemptions. Remove each
# entry when its findings have moved out or been neutralized. A strict run is
# clean only when this mapping is empty and no unclassified finding remains.
PENDING: dict[str, str] = {
    "benchmark/": "machine-specific benchmark configuration",
    "deploy/": "site-specific units and deployment defaults",
    "docs/agents/": "historical plans and specs",
    "docs/developer/": "deployment-specific developer documentation",
    "docs/superpowers/": "historical plans and specs",
    "tests/": "production-shaped fixtures awaiting neutralization or migration",
}

GUARDS = {
    "ops/admin/private_surface_sweep.py",
    "tests/unit/ops/test_private_surface_sweep.py",
}

IGNORED_VALUES = {
    "/home/agent",
    "/home/somebody",
}

_SNIFF_BYTES = 8192


def tracked_files(repo_root: Path) -> list[str]:
    """Return the paths tracked by the repository index."""
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout
    return [name.decode() for name in output.split(b"\0") if name]


def read_text(path: Path) -> str | None:
    """Read text regardless of suffix; return None only for NUL-bearing binary."""
    with path.open("rb") as stream:
        head = stream.read(_SNIFF_BYTES)
        if b"\0" in head:
            return None
        rest = stream.read()
    return (head + rest).decode("utf-8", errors="replace")


def classify(path: str) -> str | None:
    """Return the pending migration bucket that owns a path."""
    return next((prefix for prefix in PENDING if path.startswith(prefix)), None)


def sweep(repo_root: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Return pending buckets and findings that have no migration owner."""
    buckets: dict[str, set[str]] = defaultdict(set)
    violations: list[str] = []

    for relative in tracked_files(repo_root):
        if relative in GUARDS:
            continue
        try:
            text = read_text(repo_root / relative)
        except OSError as exc:
            violations.append(f"unreadable:{relative} ({exc.strerror or exc})")
            continue
        if text is None:
            continue

        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                if match.group() in IGNORED_VALUES:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                bucket = classify(relative)
                if bucket is None:
                    violations.append(f"{label}:{relative}:{line}")
                else:
                    buckets[bucket].add(relative)

    return (
        {bucket: sorted(paths) for bucket, paths in buckets.items()},
        sorted(violations),
    )


def main() -> int:
    """Print the direct-tree private-surface inventory."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero while any migration bucket or violation remains",
    )
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    buckets, violations = sweep(repo_root)

    print("pending private-surface migration buckets:")
    for prefix, paths in sorted(buckets.items()):
        print(f"  {len(paths):4d}  {prefix:36s} {PENDING[prefix]}")
    if not buckets:
        print("  (none)")

    if violations:
        print(f"\nUNCLASSIFIED ({len(violations)}):")
        for finding in violations:
            print(f"  {finding}")
    else:
        print("\nno unclassified private-surface findings")

    print(
        f"\nreadiness: {len(violations)} unclassified, "
        f"{len(buckets)} migration bucket(s) still active."
    )
    if violations or (args.strict and buckets):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
