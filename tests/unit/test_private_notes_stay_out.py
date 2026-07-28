"""Two directories of private notes must never enter the repository.

`docs/collaboration/` holds business correspondence naming people outside this
project; `.workbuddy/` holds an agent's scratch memory. Neither has ever been
committed, which is the only reason a public export cannot carry them — and
nothing but a `.gitignore` line keeps that true.

This asserts the outcome rather than the mechanism: a reordered ignore rule, a
`git add -f`, or a new sibling path all show up here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRIVATE_PREFIXES = ("docs/collaboration/", ".workbuddy/")


def test_no_private_note_is_tracked() -> None:
    """Committing one is what makes it permanent, `.gitignore` or not."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    tracked = [n.decode() for n in out.split(b"\0") if n]
    committed = [n for n in tracked if n.startswith(PRIVATE_PREFIXES)]
    assert not committed, (
        "these are private working notes and were never in the history; "
        f"committing one puts it there for good: {committed}"
    )


def test_the_ignore_rules_are_still_there() -> None:
    """Without them the next `git add .` commits the lot."""
    ignore = (REPO / ".gitignore").read_text()
    for prefix in PRIVATE_PREFIXES:
        assert prefix in ignore, f".gitignore no longer covers {prefix}"
