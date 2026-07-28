"""Show what a public export would carry, and what it would leak.

The split design settles publication as "new public repository + filtered
export". This applies the filter in ``public_export_manifest.yaml`` to the
tracked tree and then runs the public-surface audit over what survives, so the
question "is it safe to export today?" has an answer you can read rather than
argue about.

Read-only: it prints, it never writes or pushes anything.

    python ops/release/public_export.py            # summary + findings
    python ops/release/public_export.py --list     # every file that travels
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).resolve().parent / "public_export_manifest.yaml"

# The audit categories, kept in step with the tests that enforce each one:
# tests/unit/test_no_committed_credentials.py and test_no_personal_data.py.
AUDIT = {
    "gateway API key": re.compile(r"hyi-[A-Za-z0-9]{32,}"),
    "provider API key": re.compile(
        r"sk-or-v1-[A-Za-z0-9]{32,}|sk-ant-[A-Za-z0-9\-_]{40,}|sk-(?:proj-)?[A-Za-z0-9]{40,}"
    ),
    "cloud credential": re.compile(
        r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|AIza[0-9A-Za-z\-_]{35}"
    ),
    "personal mailbox": re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?:gmail|googlemail|outlook|hotmail|live|icloud|"
        r"me|yahoo|qq|163|126|foxmail)\.(?:com|me)\b",
        re.IGNORECASE,
    ),
    "internal hostname": re.compile(
        r"\b(?:internal|staging-internal)\.[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    ),
    "cluster path": re.compile(r"/n/netscratch/[A-Za-z0-9_./-]+"),
    "cloudflare identifier": re.compile(r'(?:account_id|database_id)\s*=\s*"[0-9a-f-]{32,}"'),
}

# Values that match a category but are demonstrably fixtures. Each is split so
# the literal does not itself match — this file is scanned, by gitleaks in CI
# and by the audit below, and writing a credential shape out whole trips both.
# (Third time this pattern bit me today; the two tests it mirrors carry the
# same note.)
FIXTURES = {
    "hyi-" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "AKIA" + "IOSFODNN7EXAMPLE",  # AWS's own documented example key
}

BINARY_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".pdf",
    ".mmdb",
    ".lock",
)


def load_manifest() -> tuple[list[dict], list[dict], list[dict]]:
    data = yaml.safe_load(MANIFEST.read_text())
    return (
        data.get("exclude") or [],
        data.get("undecided") or [],
        data.get("overlay") or [],
    )


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    return [n.decode() for n in out.split(b"\0") if n]


def excluded(name: str, rules: list[dict]) -> str | None:
    """Return the excluding path, or None if this file travels."""
    for rule in rules:
        p = rule["path"]
        if name == p.rstrip("/") or name.startswith(p if p.endswith("/") else p + "/"):
            return p
    return None


def materialize(names: list[str], overlay: list[dict], target: Path) -> None:
    """Write the tree the export would publish, replacements included.

    Auditing the source tree minus its exclusions is not the same thing: it
    never reads the files the export *adds*, so a replacement carrying the very
    content it stands in for would pass. It also cannot answer whether what
    survives still imports, builds or tests.
    """
    import shutil

    for name in names:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / name, destination, follow_symlinks=False)
    for rule in overlay:
        source = REPO / rule["source"]
        if not source.exists():
            continue
        destination = target / rule["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    # The export becomes a git repository the moment it is pushed, and several
    # checks in the suite find their files through `git ls-files`. A tree
    # without one is not the artifact: those checks silently see nothing and
    # pass, or fail for a reason that would never occur in the result.
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True)


def broken_docker_context(target: Path) -> list[tuple[str, str]]:
    """Find COPY sources the export drops out from under a Dockerfile.

    The repository's own guard against dangling paths reads the merged tree,
    where these files still exist. The export then removes another set of them
    by manifest, so an exclusion can leave a Dockerfile copying something that
    is no longer in the build context. Nothing before this point would notice:
    the audit reads content, the test suites never build an image, and the
    failure only appears the first time someone runs `docker compose build` --
    which, for a published repository, is a stranger.
    """
    broken: list[tuple[str, str]] = []
    for dockerfile in sorted((target / "deploy" / "docker").glob("Dockerfile*")):
        if dockerfile.suffix == ".dockerignore":
            continue
        for raw in dockerfile.read_text().splitlines():
            line = raw.strip()
            if not re.match(r"(?i)^COPY\s", line):
                continue
            try:
                parts = shlex.split(line)[1:]
            except ValueError:
                continue
            # `--from=` copies out of an earlier stage, not the build context.
            if any(p.startswith("--from=") for p in parts):
                continue
            sources = [p for p in parts if not p.startswith("--")][:-1]
            for src in sources:
                if any(ch in src for ch in "*?["):
                    if not list(target.glob(src)):
                        broken.append((dockerfile.name, f"{src} (matches nothing)"))
                elif not (target / src).exists():
                    broken.append((dockerfile.name, src))
    return broken


def audit(
    names: list[str], root: Path | None = None, overlay: list[dict] | None = None
) -> dict[str, list[str]]:
    """Scan the exported files, and the replacements if the tree was built."""
    base = root or REPO
    if root is not None:
        names = list(names) + [r["path"] for r in (overlay or []) if (base / r["path"]).exists()]
    findings: dict[str, list[str]] = defaultdict(list)
    for name in names:
        if name.lower().endswith(BINARY_SUFFIXES):
            continue
        try:
            text = (base / name).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, rx in AUDIT.items():
            for m in rx.finditer(text):
                if m.group() in FIXTURES:
                    continue
                line = text.count("\n", 0, m.start()) + 1
                findings[label].append(f"{name}:{line}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print every exported path")
    parser.add_argument(
        "--materialize",
        metavar="DIR",
        help=(
            "build the real export tree in DIR (must be empty or absent) and "
            "audit that, instead of auditing the source tree minus exclusions"
        ),
    )
    args = parser.parse_args()

    rules, undecided, overlay = load_manifest()

    # A path that is untracked by design (private notes) is legitimately absent
    # from a fresh clone; it is listed so a directory-copy export drops it too.
    stale = [
        r["path"]
        for r in rules + undecided
        if not r.get("optional") and not (REPO / r["path"].rstrip("/")).exists()
    ]
    if stale:
        print("Manifest names paths that do not exist — fix these first:")
        for p in stale:
            print(f"  {p}")
        return 2

    kept, dropped = [], defaultdict(int)
    for name in tracked_files():
        rule = excluded(name, rules)
        if rule:
            dropped[rule] += 1
        else:
            kept.append(name)

    print(f"Tracked files: {len(kept) + sum(dropped.values())}")
    print(f"  exported:    {len(kept)}")
    print(f"  excluded:    {sum(dropped.values())}")
    for path in sorted(dropped, key=lambda p: -dropped[p]):
        print(f"      {dropped[path]:5}  {path}")

    if overlay:
        print(f"\n  added by the export: {len(overlay)}")
        for rule in overlay:
            print(f"      {rule['path']}  <- {rule['source']}")
        missing = [r for r in overlay if not (REPO / r["source"]).exists()]
        pending = [r for r in missing if r.get("requires")]
        broken = [r for r in missing if not r.get("requires")]
        for r in pending:
            print(f"      (waiting on {r['requires']} for {r['source']})")
        if broken:
            print("\nOverlay sources are missing — the export would add nothing:")
            for r in broken:
                print(f"  {r['source']}")
            return 2

    if undecided:
        print(
            f"\n{len(undecided)} path(s) undecided — the export cannot run until they are settled:"
        )
        for rule in undecided:
            print(f"  {rule['path']}\n      {' '.join(rule['question'].split())}")

    stale_requires = [r for r in overlay if r.get("requires") and (REPO / r["source"]).exists()]
    if stale_requires:
        print("\nOverlay sources have arrived; drop their `requires` markers:")
        for r in stale_requires:
            print(f"  {r['path']}  (was waiting on {r['requires']})")

    if args.materialize:
        target = Path(args.materialize)
        if target.exists() and any(target.iterdir()):
            print(f"\n{target} is not empty; refusing to write into it.")
            return 2
        materialize(kept, overlay, target)
        print(f"\nExport tree written to {target}")
        # Audit what was built, not the source minus its exclusions — the whole
        # point of materialising. Re-running the source audit here silently
        # replaced the result, which is how "the export tree is clean" came to
        # be measured against the wrong tree.
        findings = audit(kept, root=target, overlay=overlay)
        broken = broken_docker_context(target)
        if broken:
            print("\nThese are COPYed from the build context but the export drops them:")
            for where, src in broken:
                print(f"  {where}: {src}")
    else:
        findings = audit(kept)
        broken = []
    print()
    if not findings:
        print("Public-surface audit of the exported tree: clean.")
    else:
        print("Public-surface audit of the exported tree: FINDINGS")
        for label in sorted(findings):
            where = findings[label]
            print(f"\n  {label}: {len(where)}")
            for w in sorted(set(where))[:10]:
                print(f"    {w}")

    if args.list:
        print("\nExported paths:")
        for name in kept:
            print(f"  {name}")

    return 1 if (findings or undecided or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
