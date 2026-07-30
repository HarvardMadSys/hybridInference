"""The export manifest has to stay true, or it stops being protection.

`ops/release/public_export_manifest.yaml` decides what a filtered export into a
public repository leaves behind. Two ways it goes quietly wrong: an entry names
a path that no longer exists, and reads as cover it is not providing; or the
exported tree grows a new leak that nobody notices because nobody re-ran the
audit.

So this pins the manifest's integrity, and treats its `known_findings` list as
a ceiling — new findings fail, and an entry that stops being found has to be
deleted. The list can only shrink.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "ops" / "release" / "public_export_manifest.yaml"

sys.path.insert(0, str(REPO / "ops" / "release"))


@pytest.fixture(scope="module")
def manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text())


def test_every_excluded_path_exists(manifest: dict) -> None:
    """A stale entry reads as protection that stopped protecting."""
    missing = [
        rule["path"]
        for rule in manifest["exclude"] + manifest["undecided"]
        # Private notes are untracked by design and absent from a fresh clone;
        # they are listed so a directory-copy export drops them too.
        if not rule.get("optional") and not (REPO / rule["path"].rstrip("/")).exists()
    ]
    assert not missing, f"the manifest excludes paths that no longer exist: {missing}"


def test_every_entry_says_why(manifest: dict) -> None:
    """The reason is the part a future reader needs; the path they can see."""
    for rule in manifest["exclude"]:
        assert rule.get("reason", "").strip(), f"{rule['path']} is excluded without a reason"
    for rule in manifest["undecided"]:
        assert rule.get("question", "").strip(), f"{rule['path']} is undecided without a question"


def test_nothing_is_left_undecided(manifest: dict) -> None:
    """An open question here is a question the export cannot answer at run time.

    Both entries this list once held — how to split docs/developer/, and what
    CI an exported repository gets — are now decided in the manifest itself.
    Reopening one is allowed; leaving it open silently is not.
    """
    assert manifest.get("undecided") == [], (
        "the export refuses to run while a path is undecided; settle it in the "
        f"manifest: {[r['path'] for r in manifest['undecided']]}"
    )


def test_every_overlay_source_exists(manifest: dict) -> None:
    """A missing source means the export adds nothing where it promised to.

    One exception, and it cleans itself up: an entry may name the pull request
    its source arrives with. Once that source exists the marker is stale, and
    the assertion below deletes it by failing — the same discipline as
    `known_findings`, so the manifest cannot keep describing a past state.
    """
    for rule in manifest.get("overlay") or []:
        assert rule.get("reason", "").strip()
        exists = (REPO / rule["source"]).exists()
        requires = rule.get("requires")

        assert exists, (
            f"{rule['path']} is supposed to come from {rule['source']}, which is "
            "gone. A `requires:` marker does not make this safe: the export "
            "writes a tree with nothing at that path and reports success."
        )
        assert not requires, (
            f"{rule['path']} still says it is waiting on {requires}, and its "
            f"source {rule['source']} is here. Delete the marker — left in, it "
            "is a note saying 'not yet' attached to a file that arrived, which "
            "is how a manifest stops being read."
        )


def test_the_exported_ci_runs_where_anyone_can_reach_it(manifest: dict) -> None:
    """The reason this repository's own CI cannot travel.

    Every job in `.github/workflows/ci.yml` here targets self-hosted runners
    this deployment owns. Exported as-is, a contributor's pull request would
    queue forever — or, worse, run their code on those machines.
    """
    overlay = {r["path"]: r for r in (manifest.get("overlay") or [])}
    rule = overlay.get(".github/workflows/ci.yml")
    assert rule, "the export must supply a CI workflow, or the public repo has none"

    workflow = yaml.safe_load((REPO / rule["source"]).read_text())
    runners = {job["runs-on"] for job in workflow["jobs"].values()}
    assert all(isinstance(r, str) and r.startswith("ubuntu-") for r in runners), (
        f"exported CI must run on GitHub-hosted runners, got {runners}"
    )
    assert "secrets." not in (REPO / rule["source"]).read_text(), (
        "exported CI must not reference secrets a fork cannot have"
    )


def test_no_overlay_source_carries_what_it_is_replacing(manifest: dict) -> None:
    """A replacement that kept the thing it replaces is worse than none.

    Each of these exists because the file it stands in for names one
    deployment's machines. Copying the original and forgetting to change the
    part that mattered would pass every other check here.
    """
    import re

    leaks = re.compile(
        r"/n/netscratch/|internal\.freeinference\.org|\bhyi-[A-Za-z0-9]{32,}",
    )
    for rule in manifest.get("overlay") or []:
        source = REPO / rule["source"]
        if not source.exists():
            continue  # covered by test_every_overlay_source_exists
        found = sorted({m.group() for m in leaks.finditer(source.read_text())})
        assert not found, (
            f"{rule['source']} replaces {rule['path']} but still carries "
            f"{found} — the reason it exists"
        )


def test_the_design_doc_exclusions_are_present(manifest: dict) -> None:
    """These two are settled upstream of this file; losing them is a regression."""
    excluded = {rule["path"] for rule in manifest["exclude"]}
    assert "distributions/" in excluded
    assert "ops/db/analysis/" in excluded


def test_no_exported_file_reaches_into_an_excluded_directory(manifest: dict) -> None:
    """A kept test that imports excluded tooling breaks collection downstream.

    `sys.path.insert(..., REPO / "ops" / "release")` is how a test reaches the
    export tool. Written in a file the export keeps, it produces a tree whose
    default `pytest` run dies on ModuleNotFoundError before a single test runs
    — and nothing here notices, because in *this* checkout the directory is
    present. It cost one round of exactly that to add this.
    """
    import re

    import public_export

    rules = manifest["exclude"]
    kept = [n for n in public_export.tracked_files() if not public_export.excluded(n, rules)]

    # Take every string literal inside the call and join them. The path is
    # spelled several ways -- `REPO / "ops" / "release"`,
    # `Path(__file__).resolve().parents[3] / "ops" / "release"`, or one
    # "ops/release" -- and matching the spellings one at a time is how the
    # first version of this test passed while the case it was written for
    # sailed through.
    call = re.compile(r"sys\.path\.insert\((.*?)\)\s*$", re.M | re.S)
    literal = re.compile(r'["\']([A-Za-z0-9_.\-/]+)["\']')

    offenders = []
    for name in kept:
        if not name.endswith(".py"):
            continue
        text = (REPO / name).read_text(encoding="utf-8", errors="ignore")
        for args in call.findall(text):
            joined = "/".join(part.strip("/") for part in literal.findall(args))
            if joined and public_export.excluded(joined, rules):
                offenders.append(f"{name} -> {joined}")

    assert not offenders, (
        "these travel with the export but import from a directory that does "
        f"not, so the exported tree cannot collect its own tests: {offenders}"
    )


def test_every_known_finding_says_why_it_is_pending(manifest: dict) -> None:
    """An exception without a reason is an exception nobody will ever remove."""
    for entry in manifest.get("known_findings") or []:
        assert entry.get("pending", "").strip(), (
            f"{entry['where']} ({entry['what']}) is allowed to leak without "
            "saying why, or when that stops"
        )
        assert isinstance(entry.get("count", 1), int) and entry.get("count", 1) >= 1


def test_the_exported_tree_grows_no_new_leak(manifest: dict) -> None:
    """The known list is a ceiling *and* a floor, counted per occurrence.

    Comparing sets of ``(file, category)`` let a file already on the list grow
    a second leak of the same kind without anything failing — the one place a
    real key is most likely to land is next to the one already forgiven. So
    each entry carries how many occurrences it covers, and both directions
    fail: more than allowed is a new leak, fewer is an entry that has been
    fixed and has to go.
    """
    from collections import Counter

    import public_export

    rules = manifest["exclude"]
    kept = [n for n in public_export.tracked_files() if not public_export.excluded(n, rules)]
    findings = public_export.audit(kept)

    allowed = Counter()
    for entry in manifest.get("known_findings") or []:
        allowed[(entry["where"], entry["what"])] += entry.get("count", 1)
    actual = Counter(
        (where.rsplit(":", 1)[0], label) for label, places in findings.items() for where in places
    )

    grew = sorted(k for k in actual if actual[k] > allowed[k])
    assert not grew, (
        "the exported tree leaks more than the known list allows — either fix "
        f"it or raise the count with a reason it is pending: "
        f"{[(k, actual[k], allowed[k]) for k in grew]}"
    )

    resolved = sorted(k for k in allowed if actual[k] < allowed[k])
    assert not resolved, (
        "these known findings no longer occur as often as the manifest claims; "
        f"the list only shrinks, so delete or lower them: "
        f"{[(k, allowed[k], actual[k]) for k in resolved]}"
    )
