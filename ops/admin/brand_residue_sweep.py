"""One-shot brand-residue sweep for the open-source acceptance check.

Criterion ② of the v1 open-source scope is "no FreeInference/Harvard brand
residue in the neutral upstream". This tool measures it: it scans every
tracked text file for brand markers and buckets hits against an explicit
allowlist, where **each bucket is a planned work stream** (overlay content,
neutral-defaults flip, step-2 physical moves, historical docs).

This is deliberately a run-once acceptance instrument, not a CI gate: run it
while migrating to watch buckets shrink, and run it with ``--strict`` at
final acceptance — by then the transitional buckets should be deleted from
ALLOWLIST and any remaining hit fails the check.

    uv run python ops/admin/brand_residue_sweep.py            # inventory
    uv run python ops/admin/brand_residue_sweep.py --strict   # acceptance
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

BRAND_MARKERS = ("freeinference", "harvard", "madsys", "junchengyang")

# Two lists, because "brand marker" covers two different things and only one of
# them is residue.
#
# ATTRIBUTION is where naming the origin is correct and permanent: a copyright
# holder, a dependency's repository, the deployment this project runs for. It
# never empties, and asking it to was a mistake — with LICENSE and the RouteWise
# dependency URL in the same list as the acceptance criterion, criterion ② could
# not be met by any amount of work, so it could never signal anything either.
ATTRIBUTION: dict[str, str] = {
    "LICENSE": "MIT, (c) Harvard SEAS — the copyright holder is required attribution",
    "pyproject.toml": (
        "the `authors` field, and the RouteWise dependency's git URL — the "
        "org name is how the package is fetched, from a public repository"
    ),
    "uv.lock": "the same RouteWise URL, resolved",
    "README.md": (
        "names the deployment this gateway runs for, and points at it as a "
        "worked example — true, useful to a reader, and not a leak"
    ),
    "README.user.md": "the same, plus the clone URL",
}

# PENDING is residue: a work stream that has not finished moving something out
# of the neutral upstream. Delete entries as they complete. **An empty PENDING
# plus a clean --strict run is the criterion-② acceptance.**
ALLOWLIST: dict[str, str] = {
    "distributions/": "distribution overlay — the intended home for brand content",
    "docs/agents/": "historical design docs (not shipped)",
    "docs/superpowers/": "historical design docs (not shipped)",
    "docs/reviews/": "review records (not shipped)",
    "docs/developer/": "internal doc-site pages — step-2 / neutral wave",
    "services/freeinference-harness/": "step-2 move/neutralize per ownership inventory",
    "services/status-monitor-worker/": "step-2 move/neutralize per ownership inventory",
    "services/alert-control-plane-worker/": "step-2 move/neutralize per ownership inventory",
    "ops/": "operator scripts — step-2 classification per ownership inventory",
    "deploy/": "deployment defaults — neutral-defaults flip wave",
    "apps/frontend/": "compile-time branding defaults — neutral-defaults flip wave",
    "apps/backend/": "Settings/RAG deployment defaults + docstrings — neutral-defaults flip wave",
    "tests/": "frozen contract values — flipped together with the neutral-defaults PR",
    "README.developer.md": "neutral README task",
    "CLAUDE.md": "mixed agent guide — site lines move with the neutral wave",
    "AGENTS.md": "mixed agent guide — site lines move with the neutral wave",
    ".github/workflows/": "FreeInference CD workflows — step-2 move",
    ".env.oncall.example": "on-call site config example — step-2 move",
    ".env.example": "one commented overlay-manifest path, which is the example that works",
    ".codex/": "agent skill guides — mixed, neutral wave",
    ".kilo/": "agent skill guides — mixed, neutral wave",
    ".gitleaks.toml": "site-specific scan allowances — neutral wave",
    "benchmark/": "paper artifacts — step-2 per ownership inventory",
    "Makefile": (
        "one pointer to docs/developer/freeinference.md — that doc is itself "
        "allowlisted and moves in step 2; the reference goes with it, and the "
        "dangling-path guard fails the pull request that forgets"
    ),
    "docs/openrouter.md": "developer note — neutral wave",
}


def classify(path: str) -> str | None:
    """Return the allowlist bucket covering ``path``, or None (violation).

    Entries ending in ``/`` are directory prefixes; anything else must match
    the path exactly (so ``LICENSE`` does not swallow ``LICENSE-THIRD-PARTY``).
    """
    for entry in {**ATTRIBUTION, **ALLOWLIST}:
        if entry.endswith("/"):
            if path.startswith(entry):
                return entry
        elif path == entry:
            return entry
    return None


def tracked_files(repo_root: Path) -> list[str]:
    """List tracked files from git (the sweep never scans untracked noise)."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout
    return [p.decode() for p in out.split(b"\0") if p]


def file_has_marker(path: Path) -> bool:
    """Whether the file mentions any brand marker (binary files are skipped)."""
    try:
        text = path.read_text(encoding="utf-8", errors="strict").lower()
    except (UnicodeDecodeError, OSError):
        return False
    return any(marker in text for marker in BRAND_MARKERS)


def sweep(repo_root: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Scan tracked files; return (bucketed hits, violations outside buckets)."""
    buckets: dict[str, list[str]] = defaultdict(list)
    violations: list[str] = []
    for rel in tracked_files(repo_root):
        if not file_has_marker(repo_root / rel):
            continue
        bucket = classify(rel)
        if bucket is None:
            violations.append(rel)
        else:
            buckets[bucket].append(rel)
    return dict(buckets), violations


def main() -> int:
    """Run the sweep and print the per-bucket inventory."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero while any residue remains (attribution does not count)",
    )
    parser.add_argument(
        "--tree",
        metavar="DIR",
        help=(
            "sweep a materialised export instead of this repository. This is "
            "the measurement that decides whether the published artifact "
            "carries residue; the repository keeps the overlay by design, so "
            "sweeping it answers a different question"
        ),
    )
    args = parser.parse_args()
    repo_root = Path(args.tree) if args.tree else Path(__file__).resolve().parents[2]

    buckets, violations = sweep(repo_root)
    total = sum(len(files) for files in buckets.values())
    print(f"brand markers: {', '.join(BRAND_MARKERS)}")
    print(f"allowlisted hits: {total} files across {len(buckets)} buckets\n")
    pending = {p: f for p, f in buckets.items() if p in ALLOWLIST}
    attribution = {p: f for p, f in buckets.items() if p in ATTRIBUTION}

    if attribution:
        print("attribution (permanent — naming the origin is correct here):")
        for prefix in sorted(attribution, key=lambda p: -len(attribution[p])):
            print(f"  {len(attribution[prefix]):4d}  {prefix:42s} {ATTRIBUTION[prefix]}")
        print()
    print("pending (residue — criterion \u2461 is met when this is empty):")
    for prefix in sorted(pending, key=lambda p: -len(pending[p])):
        print(f"  {len(pending[prefix]):4d}  {prefix:42s} {ALLOWLIST[prefix]}")
    if not pending:
        print("  (none)")

    if violations:
        print(f"\nOUTSIDE allowlist ({len(violations)}):")
        for path in sorted(violations):
            print(f"  {path}")
    else:
        print("\nno hits outside the allowlist")

    remaining = sorted(p for p in buckets if p in ALLOWLIST)
    print(
        f"\ncriterion \u2461: {len(violations)} unclaimed, "
        f"{len(remaining)} work stream(s) still to finish"
        + (" — met." if not violations and not remaining else ".")
    )

    if args.strict and (violations or remaining):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
