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

        if not exists:
            assert requires, (
                f"{rule['path']} is supposed to come from {rule['source']}, which "
                "is gone — either restore it or say which PR brings it"
            )
            continue

        assert not requires, (
            f"{rule['source']} exists now, so `requires: {requires}` on "
            f"{rule['path']} is stale — delete it"
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


def test_the_exported_tree_grows_no_new_leak(manifest: dict) -> None:
    """New findings fail here; known ones are listed with why they are pending."""
    import public_export

    rules = manifest["exclude"]
    kept = [n for n in public_export.tracked_files() if not public_export.excluded(n, rules)]
    findings = public_export.audit(kept)

    allowed = {(f["where"], f["what"]) for f in manifest.get("known_findings", [])}
    actual = {
        (where.rsplit(":", 1)[0], label) for label, places in findings.items() for where in places
    }

    new = sorted(actual - allowed)
    assert not new, (
        "the exported tree would leak something not on the known list — either "
        f"fix it or add it with a reason it is pending: {new}"
    )

    # An entry nobody finds any more is a note about the past pretending to be
    # about the present.
    resolved = sorted(allowed - actual)
    assert not resolved, (
        "these known findings no longer occur; delete them from the manifest "
        f"so the list keeps shrinking: {resolved}"
    )
