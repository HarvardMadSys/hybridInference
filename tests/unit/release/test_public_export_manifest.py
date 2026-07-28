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
