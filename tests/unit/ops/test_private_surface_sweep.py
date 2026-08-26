"""Tests for the direct-tree private-surface migration guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from ops.admin import private_surface_sweep as sweep_module

REPO = Path(__file__).resolve().parents[3]

_SAMPLES = {
    "personal mailbox": "alice@" + "gmail.com",
    "personal home path": "/Us" + "ers/alice/work/repo",
    "internal hostname": "spar" + "k2",
    "cluster path": "/n/net" + "scratch/alice/models/x",
    "cloudflare identifier": 'account_id = "' + "a" * 32 + '"',
}


@pytest.mark.parametrize("label", sorted(_SAMPLES))
def test_each_private_surface_shape_is_detected(label: str) -> None:
    """Every retained detector has a negative test that proves it can fire."""
    assert sweep_module.PATTERNS[label].search(_SAMPLES[label])


@pytest.mark.parametrize("value", ["/home/agent", "/home/somebody"])
def test_stable_service_and_placeholder_homes_are_not_personal(value: str) -> None:
    """The guard stays focused on copied developer-machine paths."""
    assert value in sweep_module.IGNORED_VALUES


@pytest.mark.parametrize("name", ["logo.svg", "uv.lock"])
def test_text_files_are_read_regardless_of_suffix(tmp_path: Path, name: str) -> None:
    """An SVG comment or lock-file URL is part of the publication surface."""
    path = tmp_path / name
    path.write_text(_SAMPLES["cluster path"])
    assert sweep_module.read_text(path) == path.read_text()


def test_binary_files_are_skipped_by_content(tmp_path: Path) -> None:
    """A NUL sniff distinguishes binaries without a bypassable suffix list."""
    path = tmp_path / "renamed-binary"
    path.write_bytes(b"prefix\0" + _SAMPLES["cluster path"].encode())
    assert sweep_module.read_text(path) is None


def test_an_unreadable_file_is_a_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A path the guard could not read was not cleared for publication."""
    monkeypatch.setattr(sweep_module, "tracked_files", lambda _root: ["missing.txt"])
    _buckets, violations = sweep_module.sweep(tmp_path)
    assert violations and violations[0].startswith("unreadable:missing.txt")


def test_current_tree_has_no_unclassified_private_surface() -> None:
    """Every current finding has an explicit migration owner."""
    buckets, violations = sweep_module.sweep(REPO)
    assert not violations
    assert set(buckets) == set(sweep_module.PENDING), (
        "remove empty PENDING entries when their migration finishes"
    )
