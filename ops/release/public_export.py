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
import importlib.util
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).resolve().parent / "public_export_manifest.yaml"

# Every command here is a local git read that finishes in milliseconds; a
# minute means something is wrong (a lock, a network filesystem stall) and
# waiting forever turns a release check into a hung terminal.
GIT_TIMEOUT_SEC = 60

_SECRET_PATTERNS = REPO / "ops" / "release" / "secret_patterns.py"


def _shared_credential_patterns() -> dict[str, re.Pattern[str]]:
    """Load the credential shapes this audit refuses to publish.

    One hand-written list, not two: the drift between two is invisible until
    the one that matters misses something. This audit once had no Slack token,
    no PEM header, and wanted a longer key than a real one has.

    These lived in the agent sandbox's patch gate until that moved to its own
    repository (task H4) — which is exactly why they now have a home of their
    own here rather than being loaded from whatever file happens to hold them.
    A scanner whose pattern source can be deleted by an unrelated change is a
    scanner that silently stops scanning while the green check keeps arriving.

    Loading by path keeps this script standalone: it runs as
    `python ops/release/public_export.py`, with no PYTHONPATH and no installed
    package, so the module it loads may import nothing beyond the standard
    library. A test pins that.
    """
    spec = importlib.util.spec_from_file_location("_hi_secret_patterns", _SECRET_PATTERNS)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable in-tree
        raise SystemExit(f"cannot load credential patterns from {_SECRET_PATTERNS}")
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves annotations through sys.modules[cls.__module__], so
    # a module executed without being registered there raises on its first
    # decorated class rather than importing.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return {f"credential ({name})": pattern for name, pattern in module.SECRET_PATTERNS}


# The audit categories, kept in step with the tests that enforce each one:
# tests/unit/test_no_committed_credentials.py and test_no_personal_data.py.
AUDIT = {
    **_shared_credential_patterns(),
    "personal mailbox": re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?:gmail|googlemail|outlook|hotmail|live|icloud|"
        r"me|yahoo|qq|163|126|foxmail)\.(?:com|me)\b",
        re.IGNORECASE,
    ),
    "personal home path": re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/[A-Za-z0-9._-]+"),
    "internal hostname": re.compile(
        r"\b(?:internal|staging-internal)\.[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        # The deployment's own machines, named directly. These are not
        # fully-qualified, so the pattern above never saw them.
        r"|\b(?:spark2|h200|holygpu\d*[a-z0-9]*)\b"
    ),
    # `/n/netscratch` is this cluster's absolute form, but the same paths get
    # written relative to a mount point (`/netscratch/...`) or as a plain
    # scratch directory, and those were travelling.
    "cluster path": re.compile(r"(?:/n)?/(?:net)?scratch/[A-Za-z0-9_./-]+"),
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
    # Placeholders and obviously-fake keys that the widened patterns now reach.
    # Each says what it is in the value itself, which is the property that
    # makes it safe to list here.
    "xoxb-" + "replace-me",
    "hyi-" + "anthropic-compat-test",
    "hyi-" + "testkey01-FULL-SECRET-VALUE",
    # The sandbox service account and its explicit test placeholder are not
    # developer machine paths. Keep the personal-home rule focused on values
    # copied from a real environment.
    "/home/" + "agent",
    "/home/" + "somebody",
}

# Files whose subject *is* credential detection, and which therefore have to
# carry samples of every shape to test anything. Naming the files is more
# honest than listing each sample literal in FIXTURES and pretending they are
# incidental; a test pins that this list stays short and that each entry really
# is a scanner test.
SCANNER_TEST_FILES = frozenset(
    {
        "tests/unit/test_no_committed_credentials.py",
    }
)

# How much of a file to look at before deciding it is not text. A NUL byte is
# the standard signal, and every real binary here has one well inside this.
_SNIFF_BYTES = 8192


def _read_text(path: Path) -> str | None:
    """Return the file's text, or None if it is genuinely binary.

    This used to be a suffix list, which got two things wrong in the same
    direction. `.svg` is XML — it holds hostnames, addresses and, in an
    exported icon set, whatever the author pasted. `.lock` is the resolver's
    output, and a private index or a URL with credentials in it lands there.
    Both were skipped by name while being perfectly readable text.

    Raises OSError, which the caller must treat as a failure rather than a
    skip: a file the audit could not read is a file the audit did not clear.
    """
    with path.open("rb") as fh:
        head = fh.read(_SNIFF_BYTES)
        if b"\x00" in head:
            return None
        rest = fh.read()
    return (head + rest).decode("utf-8", errors="replace")


def load_manifest() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    data = yaml.safe_load(MANIFEST.read_text())
    return (
        data.get("exclude") or [],
        data.get("undecided") or [],
        data.get("overlay") or [],
        data.get("keep") or [],
    )


def _git(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run a git command, and say which one failed when it does."""
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, check=True, timeout=GIT_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"timed out after {GIT_TIMEOUT_SEC}s: {shlex.join(cmd)} (in {cwd})"
        ) from None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        raise SystemExit(f"{shlex.join(cmd)} failed in {cwd}: {detail}") from None


def tracked_files() -> list[str]:
    return [n.decode() for n in _git(["git", "ls-files", "-z"], cwd=REPO).stdout.split(b"\0") if n]


def _match_depth(rule: dict, name: str) -> int | None:
    """Return how specifically ``rule`` covers ``name``, or None if it does not.

    Specificity is the rule's own length in path segments, so a rule naming one
    file outranks a rule naming the directory above it.
    """
    p = rule["path"].rstrip("/")
    if name == p or name.startswith(p + "/"):
        return len(p.split("/"))
    return None


def excluded(name: str, rules: list[dict], keep: list[dict]) -> str | None:
    """Return the excluding path, or None if this file travels.

    ``keep`` carves a named path back out of a broader exclusion, and is checked
    first. It exists so a directory can stay excluded by default while one entry
    in it travels: `distributions/` is every deployment's private overlay, but
    `distributions/example/` is the public tutorial and has to reach the people
    the tutorial is for. Inverting that -- listing the real deployments to
    exclude instead -- would publish the next one somebody adds.

    ``keep`` is required rather than defaulting to empty. A caller that omits it
    does not get a stricter answer, it gets a *wronger* one: the files it forgets
    are the ones the export actually publishes. That is not hypothetical -- the
    security audits called this with two arguments and cleared 868 files while
    877 travelled, leaving the entire example unscanned.

    The most specific rule wins, and an exclusion wins a tie. Checking ``keep``
    first instead made an exception permanent: with `keep: distributions/example/`
    in place, an `exclude:` naming one file inside it -- a private env, a key
    that should never have been committed -- was simply not applied, and the
    narrower rule sat in the manifest reading as protection.
    """
    deepest_keep = max(
        (depth for rule in keep if (depth := _match_depth(rule, name)) is not None),
        default=-1,
    )
    excluding, deepest_exclude = None, -1
    for rule in rules:
        depth = _match_depth(rule, name)
        if depth is not None and depth > deepest_exclude:
            excluding, deepest_exclude = rule["path"], depth

    if excluding is None or deepest_exclude < deepest_keep:
        return None
    return excluding


def partition_files(rules: list[dict], keep: list[dict]) -> tuple[list[str], dict[str, int]]:
    """Split tracked files into what travels and what each rule dropped."""
    kept: list[str] = []
    dropped: dict[str, int] = defaultdict(int)
    for name in tracked_files():
        rule = excluded(name, rules, keep)
        if rule:
            dropped[rule] += 1
        else:
            kept.append(name)
    return kept, dropped


def export_plan() -> dict[str, Path]:
    """Map every published path to the file whose content lands there.

    This is the export as a value: the tracked files that survive the manifest,
    plus the overlay replacements, with a replacement winning the path it
    stands in for. Anything that rebuilds part of it -- a filtered list of
    tracked names -- describes the export rather than being it, and the
    difference is not cosmetic. The audits did exactly that and cleared 877
    files while 882 were published: five overlay-only files were never opened,
    and two more were read as the content they replace.
    """
    rules, _undecided, overlay, keep = load_manifest()
    plan: dict[str, Path] = {name: REPO / name for name in partition_files(rules, keep)[0]}
    for rule in overlay:
        plan[rule["path"]] = REPO / rule["source"]
    return dict(sorted(plan.items()))


def exported_files() -> list[str]:
    """Return every path the export publishes, replacements included."""
    return list(export_plan())


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
        # Never follow a link out of the tree. A tracked symlink pointing at
        # something outside the repository would otherwise be published as a
        # copy of whatever it aimed at.
        shutil.copy2(REPO / name, destination, follow_symlinks=False)
    for rule in overlay:
        source = REPO / rule["source"]
        if not source.exists():
            # Callers refuse to reach here; assert it rather than skipping,
            # which is how a tree with no config/models.yaml got written and
            # then reported as a successful export.
            raise SystemExit(f"overlay source {rule['source']} is missing; refusing to write")
        destination = target / rule["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)

    # The export becomes a git repository the moment it is pushed, and several
    # checks in the suite find their files through `git ls-files`. A tree
    # without one is not the artifact: those checks silently see nothing and
    # pass, or fail for a reason that would never occur in the result.
    _git(["git", "init", "-q"], cwd=target)
    _git(["git", "add", "-A"], cwd=target)

    # Read back what was written. Everything above is a plan; this is the only
    # statement about the artifact.
    absent = [r["path"] for r in overlay if not (target / r["path"]).exists()]
    if absent:
        raise SystemExit("overlay replacements did not land: " + ", ".join(absent))


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
    names: list[str],
    root: Path | None = None,
    overlay: list[dict] | None = None,
    sources: dict[str, Path] | None = None,
) -> dict[str, list[str]]:
    """Scan the exported files, and the replacements if the tree was built.

    Unreadable files are reported under their own category rather than skipped.
    A permission error or a copy that did not land used to `continue`, and the
    run still ended in "clean" — the one word this tool exists to be trusted
    about. Whatever it could not read, it did not clear.

    ``sources`` maps a published path to the file whose content lands there, so
    a caller with an :func:`export_plan` audits replacements without building
    the tree. Findings are still reported under the published path, which is
    where a reader would go looking.
    """
    base = root or REPO
    if root is not None:
        names = list(names) + [r["path"] for r in (overlay or []) if (base / r["path"]).exists()]
    findings: dict[str, list[str]] = defaultdict(list)
    for name in names:
        origin = (sources or {}).get(name, base / name)
        try:
            text = _read_text(origin)
        except OSError as exc:
            findings["unreadable (audit could not clear it)"].append(f"{name}: {exc.strerror}")
            continue
        if text is None:
            continue
        scanner_test = name in SCANNER_TEST_FILES
        for label, rx in AUDIT.items():
            if scanner_test and label.startswith("credential ("):
                continue
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

    rules, undecided, overlay, keep = load_manifest()

    # A path that is untracked by design (private notes) is legitimately absent
    # from a fresh clone; it is listed so a directory-copy export drops it too.
    stale = [
        r["path"]
        for r in rules + undecided + keep
        if not r.get("optional") and not (REPO / r["path"].rstrip("/")).exists()
    ]
    if stale:
        print("Manifest names paths that do not exist — fix these first:")
        for p in stale:
            print(f"  {p}")
        return 2

    kept, dropped = partition_files(rules, keep)
    # Everything the run reports on or audits comes from here, so the CLI cannot
    # drift from the artifact the way its two entry points had: the default
    # audit and `--list` both described the filtered source tree.
    plan = export_plan()

    print(f"Tracked files: {len(kept) + sum(dropped.values())}")
    print(f"  exported:    {len(kept)}")
    print(f"  excluded:    {sum(dropped.values())}")
    for path in sorted(dropped, key=lambda p: -dropped[p]):
        print(f"      {dropped[path]:5}  {path}")

    if overlay:
        print(f"\n  added by the export: {len(overlay)}")
        for rule in overlay:
            print(f"      {rule['path']}  <- {rule['source']}")
        # A `requires:` marker records which pull request brings the source. It
        # is scheduling information, not permission to export without it: the
        # tree that comes out has no file at that path, and used to come out
        # with exit 0 and the word "clean".
        missing = [r for r in overlay if not (REPO / r["source"]).exists()]
        if missing:
            print("\nOverlay sources are missing — the export would add nothing at:")
            for r in missing:
                waiting = f"  (expected from {r['requires']})" if r.get("requires") else ""
                print(f"  {r['path']}  <- {r['source']}{waiting}")
            return 2

    if undecided:
        print(
            f"\n{len(undecided)} path(s) undecided — the export cannot run until they are settled:"
        )
        for rule in undecided:
            print(f"  {rule['path']}\n      {' '.join(rule['question'].split())}")

    # Every source exists by this point, so any surviving marker describes a
    # state the repository has left. Left in, it is a note saying "not here
    # yet" attached to a file that is — which is exactly the kind of stale
    # bookkeeping that makes a manifest stop being read.
    stale_requires = [r for r in overlay if r.get("requires")]
    if stale_requires:
        print("\nOverlay sources have arrived; drop their `requires` markers:")
        for r in stale_requires:
            print(f"  {r['path']}  (was waiting on {r['requires']})")
        return 2

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
        findings = audit(list(plan), root=target)
        broken = broken_docker_context(target)
        if broken:
            print("\nThese are COPYed from the build context but the export drops them:")
            for where, src in broken:
                print(f"  {where}: {src}")
    else:
        # Same set of published paths, read through the plan so replacements are
        # scanned as their replacing content. This default path is the one an
        # operator actually runs, and it was the last place still auditing the
        # source tree minus exclusions: five overlay-only files never opened,
        # two read as the content they stand in for.
        findings = audit(list(plan), sources=plan)
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
        for name in plan:
            print(f"  {name}")

    return 1 if (findings or undecided or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
