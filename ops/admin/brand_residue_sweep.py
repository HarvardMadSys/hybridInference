"""One-shot brand-residue sweep for the open-source acceptance check.

Criterion ② of the v1 open-source scope is "no FreeInference/Harvard brand
residue in the neutral upstream". This tool measures it: it scans every
tracked text file for brand markers and buckets hits against an explicit
allowlist, where **each bucket is a planned work stream** (overlay content,
neutral-defaults flip, step-2 physical moves, historical docs).

This is deliberately a migration acceptance instrument, not a CI gate: run it
while migrating to watch buckets shrink, and run it with ``--strict`` at final
acceptance — by then the transitional buckets should be deleted from ALLOWLIST
and any remaining hit fails the check. It always scans the repository that
will be made public; there is no filtered publication tree.

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
# holder, an authors field, the deployment this project runs for. It never
# empties, and asking it to was a mistake — with LICENSE in the same list as the
# acceptance criterion, criterion ② could not be met by any amount of work, so
# it could never signal anything either.
ATTRIBUTION: dict[str, str] = {
    "LICENSE": "MIT, (c) Harvard SEAS — the copyright holder is required attribution",
    "pyproject.toml": "the `authors` field",
    "README.md": (
        "names the deployment this gateway runs for, and points at it as a "
        "worked example — true, useful to a reader, and not a leak"
    ),
    "README.user.md": "the same, plus the clone URL",
    "README.developer.md": "the clone URL, and the same worked example",
    "deploy/docker/docker-compose.yml": (
        "NEXT_PUBLIC_GITHUB_URL defaults to this repository, which is where "
        "the console's source link should point"
    ),
    "apps/frontend/src/config/branding.ts": (
        "the same default, and comments naming this deployment as the example "
        "for why each neutral default is what it is"
    ),
    "docs/developer/router-tutorial.md": "the clone URL, same as installation.md",
    "CLAUDE.md": (
        "links to the public sibling cloud-agent repository, cited the way a dependency is"
    ),
    "AGENTS.md": (
        "links to the public sibling cloud-agent repository, cited the way a dependency is"
    ),
}

# GUARDS are files whose job is to notice these markers. They have to contain
# them: a test asserting a marker is absent quotes it, and the list of markers
# lives here. Counting them as residue meant the criterion could not reach zero
# for the same reason ATTRIBUTION could not — the thing being measured includes
# the measuring apparatus.
GUARDS: dict[str, str] = {
    "ops/admin/brand_residue_sweep.py": "defines BRAND_MARKERS",
    "ops/admin/private_surface_sweep.py": (
        "defines the transitional FreeInference private-surface migration bucket"
    ),
    "tests/unit/ops/test_brand_residue_sweep.py": "exercises the classifier above",
    "tests/unit/ops/test_private_surface_sweep.py": (
        "exercises the private-surface migration guard"
    ),
    "tests/servers/test_neutral_startup.py": "asserts no marker reaches a response",
    "tests/unit/config/test_site_identity.py": "asserts the identity names no deployment",
    "tests/unit/config/test_contract_settings_defaults.py": "asserts no marker in the CORS default",
    "tests/unit/deploy/test_compose_identity.py": "asserts compose defaults name no deployment",
    "tests/unit/test_no_personal_data.py": "carries the address shapes it scans for",
    "tests/unit/rag/test_rag_config.py": (
        "its offender scan quotes the marker it forbids in source"
    ),
}

# PENDING is residue: a work stream that has not finished moving something out
# of the neutral upstream. Delete entries as they complete. **An empty PENDING
# plus a clean --strict run is the criterion-② acceptance.**
ALLOWLIST: dict[str, str] = {
    "docs/agents/": "historical design docs — review before direct publication",
    "docs/superpowers/": "historical design docs — review before direct publication",
    "docs/reviews/": "review records — review before direct publication",
    "services/freeinference-harness/": (
        "generic protocol-conformance testkit; neutral naming is tracked separately"
    ),
    "ops/": "operator scripts — step-2 classification per ownership inventory",
    "deploy/": "deployment defaults — neutral-defaults flip wave",
    "apps/frontend/": "compile-time branding defaults — neutral-defaults flip wave",
    "apps/backend/": "Settings/RAG deployment defaults + docstrings — neutral-defaults flip wave",
    "tests/": "frozen contract values — flipped together with the neutral-defaults PR",
    ".github/workflows/": (
        "the GHCR namespace, the internal doc-site host, and a runner-billing "
        "note — neutral-defaults flip wave"
    ),
    ".env.oncall.example": "on-call site config example — step-2 move",
    ".env.example": "one commented overlay-manifest path, which is the example that works",
    "benchmark/": "paper artifacts — step-2 per ownership inventory",
    "Makefile": "a comment naming the sibling cloud-agent repository",
}


def classify(path: str) -> str | None:
    """Return the allowlist bucket covering ``path``, or None (violation).

    Entries ending in ``/`` are directory prefixes; anything else must match
    the path exactly (so ``LICENSE`` does not swallow ``LICENSE-THIRD-PARTY``).
    """
    for entry in {**ATTRIBUTION, **GUARDS, **ALLOWLIST}:
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
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[2]

    buckets, violations = sweep(repo_root)
    total = sum(len(files) for files in buckets.values())
    print(f"brand markers: {', '.join(BRAND_MARKERS)}")
    print(f"allowlisted hits: {total} files across {len(buckets)} buckets\n")
    pending = {p: f for p, f in buckets.items() if p in ALLOWLIST}
    attribution = {p: f for p, f in buckets.items() if p in ATTRIBUTION}
    guards = {p: f for p, f in buckets.items() if p in GUARDS}

    for title, group, reasons in (
        ("attribution (permanent — naming the origin is correct here)", attribution, ATTRIBUTION),
        ("guards (must contain the markers to notice them)", guards, GUARDS),
    ):
        if group:
            print(f"{title}:")
            for prefix in sorted(group, key=lambda p: -len(group[p])):
                print(f"  {len(group[prefix]):4d}  {prefix:42s} {reasons[prefix]}")
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
