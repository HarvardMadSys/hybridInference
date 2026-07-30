"""The export audit has to fail on the things it exists to catch.

Every check here corresponds to a way the tool reported "clean" while missing
something real: a credential shape it had never been taught, a file it declined
to read, a file it could not read, a cluster path written without the leading
mount point, and an overlay replacement that never landed. A scanner that
cannot be shown failing is a scanner nobody should trust.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ops" / "release"))

import public_export

# Split so this file does not trip the very scanners it tests, nor gitleaks.
PLANTED = {
    "slack bot token": "xoxb-" + "1234567890" + "-planted-by-a-test",
    "agent worker token": "ajt." + "abcdefghij" + "." + "klmnopqrst",
    "openai key": "sk-" + "A" * 24,
    "gateway key": "hyi-" + "B" * 24,
    "pem header": "-----BEGIN " + "PRIVATE KEY-----",
}


@pytest.mark.parametrize("label", sorted(PLANTED))
def test_a_planted_credential_is_found(tmp_path: Path, label: str) -> None:
    """Each shape the shared list knows about, proven to be reachable here.

    These are the formats the audit had no pattern for: it carried its own
    hand-written list, and Slack tokens, agent-worker tokens and PEM headers
    were simply not on it.
    """
    (tmp_path / "carrier.txt").write_text(f"token = {PLANTED[label]}\n")

    findings = public_export.audit(["carrier.txt"], root=tmp_path)

    assert findings, f"{label} passed the audit"
    assert any(w.startswith("carrier.txt:") for places in findings.values() for w in places)


@pytest.mark.parametrize("name", ["logo.svg", "uv.lock"])
def test_text_files_with_binary_looking_names_are_read(tmp_path: Path, name: str) -> None:
    """`.svg` is XML and `.lock` is a resolver's text output.

    Both were skipped by suffix. An icon set carries whatever the author
    pasted into a comment, and a lock file carries index URLs — which is where
    a credential embedded in a private registry URL would sit.
    """
    (tmp_path / name).write_text(f"# {PLANTED['gateway key']}\n")

    findings = public_export.audit([name], root=tmp_path)

    assert findings, f"{name} was skipped by its extension"


def test_a_real_binary_is_still_skipped(tmp_path: Path) -> None:
    """The replacement is a NUL sniff, not "read everything and hope"."""
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")

    assert public_export.audit(["image.png"], root=tmp_path) == {}


def test_an_unreadable_file_is_a_finding_not_a_skip(tmp_path: Path) -> None:
    """Whatever the audit could not read, it did not clear.

    This used to `continue`, so a permission error or a copy that did not land
    ended the run in the one word this tool exists to be trusted about.
    """
    victim = tmp_path / "locked.txt"
    victim.write_text("nothing interesting")
    victim.chmod(0o000)
    try:
        findings = public_export.audit(["locked.txt"], root=tmp_path)
    finally:
        victim.chmod(0o644)

    assert any("unreadable" in label for label in findings), (
        f"an unreadable file left the audit clean: {findings}"
    )


# Assembled rather than written out, for the same reason the credential samples
# above are: this file is scanned by the audit it tests, and a literal here is
# a finding in the exported tree.
_SCRATCH = "/scr" + "atch/someone/models/x"
_MACHINE = "spar" + "k2"
_MACOS_HOME = "/Us" + "ers/example/work/hybridInference"
_LINUX_HOME = "/ho" + "me/alice/work/hybridInference"


@pytest.mark.parametrize(
    "line",
    [
        f"MODEL_DIR={_SCRATCH}",
        f"MODEL_DIR=/net{_SCRATCH.lstrip('/')}",
        f"MODEL_DIR=/n/net{_SCRATCH.lstrip('/')}",
    ],
)
def test_cluster_paths_are_found_in_every_form_they_are_written(tmp_path: Path, line: str) -> None:
    """Only the fully-qualified `/n/netscratch` form was matched.

    The same directories reach scripts relative to a mount point, and both
    spellings were travelling in the benchmark tree.
    """
    (tmp_path / "run.sh").write_text(line + "\n")

    findings = public_export.audit(["run.sh"], root=tmp_path)

    assert "cluster path" in findings, f"{line} passed the audit"


def test_this_deployments_machines_are_found_by_bare_name(tmp_path: Path) -> None:
    """The hostname pattern required a domain; these are written bare."""
    (tmp_path / "run.sh").write_text(f"ssh {_MACHINE} nvidia-smi\n")

    assert "internal hostname" in public_export.audit(["run.sh"], root=tmp_path)


@pytest.mark.parametrize("path", [_MACOS_HOME, _LINUX_HOME])
def test_personal_home_paths_are_found(tmp_path: Path, path: str) -> None:
    """Recorded local paths disclose a developer identity and machine layout."""
    (tmp_path / "event.jsonl").write_text(f'{{"cwd": "{path}"}}\n')

    findings = public_export.audit(["event.jsonl"], root=tmp_path)

    assert "personal home path" in findings, f"{path} passed the audit"


@pytest.mark.parametrize("path", ["/home/agent", "/home/somebody"])
def test_synthetic_and_service_home_paths_are_allowed(tmp_path: Path, path: str) -> None:
    """Stable sandbox identities are not developer-machine disclosures."""
    (tmp_path / "config.txt").write_text(f"HOME={path}\n")

    assert public_export.audit(["config.txt"], root=tmp_path) == {}


def test_materialize_refuses_when_an_overlay_source_is_missing(tmp_path: Path) -> None:
    """A tree with nothing at the replaced path is not a successful export."""
    target = tmp_path / "out"
    overlay = [{"path": "config/models.yaml", "source": "does/not/exist.yaml"}]

    with pytest.raises(SystemExit, match=re.escape("does/not/exist.yaml")):
        public_export.materialize([], overlay, target)


def test_the_credential_patterns_come_from_the_agent_gate() -> None:
    """One list, so the two scanners cannot drift apart.

    They already had: the release audit knew nothing about Slack tokens,
    agent-worker tokens or PEM headers, and wanted a gateway key longer than a
    real one.
    """
    gate = REPO / "apps" / "backend" / "serving" / "agent_jobs" / "patch_gate.py"
    assert gate.exists(), "public_export.py loads its credential shapes from this file"

    source = gate.read_text()
    assert "SECRET_PATTERNS" in source

    # public_export.py loads it by path, outside any package, so an import
    # beyond the standard library here would break the release check.
    third_party = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
        and not line.startswith(("from __future__", "import re", "from dataclasses"))
    ]
    assert not third_party, (
        f"patch_gate.py grew an import the release tool cannot satisfy: {third_party}"
    )


def test_every_scanner_test_file_exists() -> None:
    """The credential-shape exemption names files; a stale name is a hole."""
    for name in public_export.SCANNER_TEST_FILES:
        assert (REPO / name).exists(), f"{name} is exempt from credential scanning but is gone"
