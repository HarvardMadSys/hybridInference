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

It guards the current tracked tree. The existing HybridInference repository is
the repository that will become public, so its complete retained Git history
is a separate pre-publication audit surface. Removing a leaked key from HEAD is
necessary but not sufficient: revoke it first, then clean every retained ref
before changing repository visibility.
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
    "HybridInference gateway API key": re.compile(r"hyi-[A-Za-z0-9_-]{32,}"),
    "Inference grant token": re.compile(r"agr\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}"),
    "Agent worker token": re.compile(r"ajt\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}"),
    "OpenRouter key": re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    "OpenAI key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{20,}"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    "Slack token": re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
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
    "ghp_abcdefghijklmnopqrstuvwxyz1234",
    "xoxb-replace-me",
    "xoxb-unit-test-token-123456",
    "xoxb-sensitive-token-value-123456",
    "xoxb-must-not-be-reflected",
}

_SNIFF_BYTES = 8192


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    names = [n.decode() for n in out.split(b"\0") if n]
    return [REPO / n for n in names]


def _read_text(path: Path) -> str | None:
    """Read tracked text regardless of suffix; skip only NUL-bearing binaries."""
    with path.open("rb") as stream:
        head = stream.read(_SNIFF_BYTES)
        if b"\0" in head:
            return None
        rest = stream.read()
    return (head + rest).decode("utf-8", errors="replace")


@pytest.mark.parametrize("label", sorted(SECRET_SHAPES))
def test_no_tracked_file_carries_a_credential(label: str) -> None:
    """One case per shape, so a failure names which kind of key leaked."""
    pattern = SECRET_SHAPES[label]
    findings: list[str] = []

    for path in _tracked_files():
        text = _read_text(path)
        if text is None:
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
    assert gateway.fullmatch("hyi-" + "A" * 31 + "_")
    assert gateway.fullmatch("hyi-" + "A" * 31 + "-")

    # Fixtures in this repository must keep passing.
    for fixture in (
        "hyi-anthropic-compat-test",
        "hyi-not-a-real-key",
        "hyi-testkey01-FULL-SECRET-VALUE",
        "hyi-abcdefghijklmnopqrstuvwxyz",
    ):
        assert not gateway.fullmatch(fixture), fixture

    grant = SECRET_SHAPES["Inference grant token"]
    worker = SECRET_SHAPES["Agent worker token"]
    payload = "abcdefghij"
    signature = "klmnopqrst"
    assert grant.fullmatch("agr." + payload + "." + signature)
    assert worker.fullmatch("ajt." + payload + "." + signature)


def test_private_key_header_is_not_globally_allowlisted() -> None:
    """A standard PKCS#8 key header must remain a publication blocker."""
    header = "-----BEGIN " + "PRIVATE KEY-----"
    private_key = SECRET_SHAPES["Private key"]

    assert private_key.fullmatch(header)
    assert header not in KNOWN_FIXTURES


@pytest.mark.parametrize("name", ["logo.svg", "uv.lock"])
def test_text_files_are_scanned_regardless_of_suffix(tmp_path: Path, name: str) -> None:
    """SVG and lock files are text and may contain credentials."""
    path = tmp_path / name
    path.write_text("hyi-" + "A" * 32)
    assert _read_text(path) == path.read_text()


def test_binary_files_are_skipped_by_content(tmp_path: Path) -> None:
    """Binary detection is based on NUL bytes rather than a filename allowlist."""
    path = tmp_path / "renamed-without-a-binary-suffix"
    path.write_bytes(b"prefix\0hyi-" + b"A" * 32)
    assert _read_text(path) is None


def test_unreadable_or_missing_files_fail_closed(tmp_path: Path) -> None:
    """A file the guard could not read was not cleared for publication."""
    with pytest.raises(OSError):
        _read_text(tmp_path / "missing.txt")
