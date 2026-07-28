"""No tracked file may carry a real credential.

CI runs gitleaks, but with two blind spots that let one through. It scans
``--no-git``, so only the working tree — nothing has ever scanned this
repository's history. And ``.gitleaks.toml`` allowlists
``docs/agents/(plans|specs)/*.md`` wholesale, on the assumption that credentials
in design documents are samples. A live gateway API key sat in an archived plan
document for months because both of those held at once.

This test closes the second gap in the repository's own language, and covers
the credential formats this project issues, which the scanner's default rules
do not know about.

It guards the tree, which is what gets published: the split design settles the
route as a filtered export into a new public repository and forbids rewriting
this repo's history, so the past stays private and the export carries the tree.
That makes removing a leaked key here worth doing — but not sufficient. Anyone
with repository access has already been able to read it, so it still has to be
revoked.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Credential shapes worth failing on. Each is deliberately narrow: enough
# random-looking payload that a hand-written fixture will not match.
SECRET_SHAPES = {
    "HybridInference gateway API key": re.compile(r"hyi-[A-Za-z0-9]{32,}"),
    "OpenRouter key": re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9\-_]{40,}"),
    "OpenAI key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{40,}"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "Slack bot token": re.compile(r"xoxb-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{20,}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
}

# Values that match a shape above but are demonstrably not credentials. Keep
# this list short and literal — a fixture that needs a random-looking key is a
# fixture that should use an obviously fake one instead.
KNOWN_FIXTURES = {
    "hyi-abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAABCDEFGHIJKLMNOP",
    # AWS publishes this one in its own documentation as the example access
    # key. It reaches this repository through test_agent_patch_gate.py, whose
    # subject is secret detection, so it necessarily carries samples.
    "AKIAIOSFODNN7EXAMPLE",
}

# Binary and vendored trees carry no hand-written credentials and cost time.
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


@pytest.mark.parametrize("label", sorted(SECRET_SHAPES))
def test_no_tracked_file_carries_a_credential(label: str) -> None:
    """One case per shape, so a failure names which kind of key leaked."""
    pattern = SECRET_SHAPES[label]
    findings: list[str] = []

    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text:
            continue
        for match in pattern.finditer(text):
            value = match.group()
            if value in KNOWN_FIXTURES:
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{path.relative_to(REPO)}:{line} ({value[:12]}...)")

    assert not findings, (
        f"{label} committed to the repository. Revoke it — everyone with "
        f"repository access has been able to read it — then remove it here: "
        f"{findings}"
    )


def test_the_shapes_actually_discriminate() -> None:
    """A pattern that matches nothing real is a test that guards nothing.

    The gateway-key shape is the one that failed in practice, so pin that it
    still separates the leaked key from the fixtures that surround it.
    """
    gateway = SECRET_SHAPES["HybridInference gateway API key"]

    # Split so the literal does not itself match the pattern above — the scan
    # reads tracked files, and this file is one of them. (It caught exactly
    # that when the sample was written out whole.)
    sample = "hyi-" + "QQ7mKp2XvR9tLbN4wZcE6yHsA1dJfG3uT8oViM5rPkB"
    assert gateway.fullmatch(sample)

    # Fixtures in this repository must keep passing.
    for fixture in (
        "hyi-anthropic-compat-test",
        "hyi-not-a-real-key",
        "hyi-testkey01-FULL-SECRET-VALUE",
        "hyi-abcdefghijklmnopqrstuvwxyz",
    ):
        assert not gateway.fullmatch(fixture), fixture
