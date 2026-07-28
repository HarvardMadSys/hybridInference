"""No tracked file may name a real person's mailbox.

The pre-publication checklist in
``docs/agents/specs/2026-06-18-opensource-decoupling.zh.md`` calls for a
public-surface audit covering "internal domains, host topology comments, real
email addresses". This is the email half.

It found four. All were in usage examples in ``ops/db/analysis/`` docstrings,
where someone had pasted the real command they ran — including a pair of
addresses passed to a script whose stated purpose is showing that two accounts
belong to one person. That is an abuse investigation of named users, recorded
in a repository, and it does not stop being personal data because it sits in a
comment.

The bar here is the mailbox, not the credential: a service's own support
address is a published fact, while a user's is theirs. So this checks the
domains where individuals actually read mail, and leaves ``@example.com``,
``@localhost`` and the project's own addresses alone.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Where individuals read mail, as opposed to where fixtures point.
PERSONAL_MAILBOX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@(?:"
    r"gmail\.com|googlemail\.com|outlook\.com|hotmail\.com|live\.com|"
    r"icloud\.com|me\.com|yahoo\.com|proton\.me|protonmail\.com|"
    r"qq\.com|163\.com|126\.com|foxmail\.com"
    r")\b",
    re.IGNORECASE,
)

SKIP_PREFIXES = ("apps/frontend/node_modules/", "var/", "tests/fixtures/data/")
SKIP_SUFFIXES = (
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


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    names = [n.decode() for n in out.split(b"\0") if n]
    return [
        REPO / n
        for n in names
        if not n.startswith(SKIP_PREFIXES) and not n.lower().endswith(SKIP_SUFFIXES)
    ]


def test_no_tracked_file_names_a_personal_mailbox() -> None:
    """A placeholder reads the same to a reader and belongs to nobody."""
    findings: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in PERSONAL_MAILBOX.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            local = match.group().split("@")[0]
            # Report enough to find it, not enough to republish it.
            findings.append(f"{path.relative_to(REPO)}:{line} ({local[:3]}…@…)")

    assert not findings, (
        "these name a real person's mailbox; use a@x.com or another placeholder "
        f"instead — the example reads the same: {findings}"
    )


def test_the_pattern_separates_people_from_fixtures() -> None:
    """A rule that flags every address would be turned off within a week."""
    # Split so these literals do not themselves match: the scan above reads
    # tracked files, and this file is one of them. (It caught exactly that when
    # they were written out whole — the same way its sibling
    # test_no_committed_credentials.py did.)
    assert PERSONAL_MAILBOX.search("someone@" + "gmail.com")
    assert PERSONAL_MAILBOX.search("Someone.Else@" + "Outlook.com")

    for benign in (
        "a@x.com",
        "admin@example.com",
        "noreply@localhost",
        "admin@freeinference.org",
        "support@some-company.io",
    ):
        assert not PERSONAL_MAILBOX.search(benign), benign
