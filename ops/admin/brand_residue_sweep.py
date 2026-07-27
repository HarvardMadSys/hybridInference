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

# path prefix (or exact file) -> why brand mentions are currently expected
# there. Delete entries as their work stream completes; an empty allowlist
# plus a clean --strict run is the criterion-② acceptance.
ALLOWLIST: dict[str, str] = {
    "distributions/": "distribution overlay — the intended home for brand content",
    "docs/agents/": "historical design docs (not shipped)",
    "docs/superpowers/": "historical design docs (not shipped)",
    "docs/reviews/": "review records (not shipped)",
    "docs/free_inference/": "transitional Pages copy — removed by the docs cleanup PR",
    "docs/developer/": "internal doc-site pages — step-2 / neutral wave",
    "services/freeinference-harness/": "step-2 move/neutralize per ownership inventory",
    "services/status-monitor-worker/": "step-2 move/neutralize per ownership inventory",
    "services/alert-control-plane-worker/": "step-2 move/neutralize per ownership inventory",
    "ops/": "operator scripts — step-2 classification per ownership inventory",
    "deploy/": "deployment defaults — neutral-defaults flip wave",
    "apps/frontend/": "compile-time branding defaults — neutral-defaults flip wave",
    "apps/backend/": "Settings/RAG deployment defaults + docstrings — neutral-defaults flip wave",
    "tests/": "frozen contract values — flipped together with the neutral-defaults PR",
    "README.md": "neutral README task",
    "README.user.md": "neutral README task",
    "README.developer.md": "neutral README task",
    "CLAUDE.md": "mixed agent guide — site lines move with the neutral wave",
    "AGENTS.md": "mixed agent guide — site lines move with the neutral wave",
    "pyproject.toml": "harness excludes + RouteWise git dep URL (public repo; org name matches markers)",
    ".github/workflows/": "FreeInference CD workflows — step-2 move",
    ".env.oncall.example": "on-call site config example — step-2 move",
    ".env.example": "example env carries FreeInference defaults — neutral-defaults flip wave",
    ".codex/": "agent skill guides — mixed, neutral wave",
    ".kilo/": "agent skill guides — mixed, neutral wave",
    ".gitleaks.toml": "site-specific scan allowances — neutral wave",
    "LICENSE": "copyright/licensing — pending license decision (owner: Murphy)",
    "benchmark/": "paper artifacts — step-2 per ownership inventory",
    "config/": "production config truth — moves to the overlay in the config-migration PR",
    "docs/openrouter.md": "developer note — neutral wave",
    "uv.lock": "RouteWise git dep URL (public repo; org name matches markers) — fine to ship",
}


def classify(path: str) -> str | None:
    """Return the allowlist bucket covering ``path``, or None (violation).

    Entries ending in ``/`` are directory prefixes; anything else must match
    the path exactly (so ``LICENSE`` does not swallow ``LICENSE-THIRD-PARTY``).
    """
    for entry in ALLOWLIST:
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
        help="exit non-zero when any hit falls outside the allowlist",
    )
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[2]

    buckets, violations = sweep(repo_root)
    total = sum(len(files) for files in buckets.values())
    print(f"brand markers: {', '.join(BRAND_MARKERS)}")
    print(f"allowlisted hits: {total} files across {len(buckets)} buckets\n")
    for prefix in sorted(buckets, key=lambda p: -len(buckets[p])):
        print(f"  {len(buckets[prefix]):4d}  {prefix:42s} {ALLOWLIST[prefix]}")

    if violations:
        print(f"\nOUTSIDE allowlist ({len(violations)}):")
        for path in sorted(violations):
            print(f"  {path}")
    else:
        print("\nno hits outside the allowlist")

    if args.strict and violations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
