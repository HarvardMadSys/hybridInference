"""Tests for the brand-residue acceptance sweep."""

from __future__ import annotations

from pathlib import Path

from ops.admin import brand_residue_sweep as sweep_module
from ops.admin.brand_residue_sweep import classify, file_has_marker

REPO = Path(__file__).resolve().parents[3]


def test_classify_matches_prefixes_and_exact_files() -> None:
    assert classify("docs/agents/plans/some-plan.md") == "docs/agents/"
    assert classify("LICENSE") == "LICENSE"
    assert classify("apps/backend/serving/config/settings.py") == "apps/backend/"


def test_classify_returns_none_outside_the_allowlist() -> None:
    assert classify("newdir/some_file.py") is None
    # Exact-file entries must not act as prefixes for lookalike names.
    assert classify("LICENSE-THIRD-PARTY") is None


def test_file_has_marker_is_case_insensitive_and_skips_binary(tmp_path) -> None:
    hit = tmp_path / "hit.md"
    hit.write_text("Built at HARVARD SEAS")
    miss = tmp_path / "miss.md"
    miss.write_text("a neutral gateway")
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\xff\xfeharvard")

    assert file_has_marker(hit) is True
    assert file_has_marker(miss) is False
    assert file_has_marker(binary) is False


def test_attribution_is_not_counted_as_residue() -> None:
    """Criterion ② has to be reachable, or it signals nothing.

    LICENSE names the copyright holder; pyproject.toml names the authors; the
    READMEs name the deployment
    this gateway runs for and point at it as a worked example. None of that can
    be removed, and all of it sat in the list whose emptiness was the
    acceptance criterion — so no amount of work could ever meet it.
    """
    assert "LICENSE" in sweep_module.ATTRIBUTION
    assert not set(sweep_module.ATTRIBUTION) & set(sweep_module.ALLOWLIST), (
        "an entry claimed as permanent attribution is also listed as pending work"
    )
    for entry, reason in sweep_module.ATTRIBUTION.items():
        assert reason.strip(), f"{entry} is exempt forever without saying why"


def test_attribution_still_classifies_so_it_is_not_a_violation() -> None:
    """Moving the entries must not turn them into unclaimed hits."""
    for entry in sweep_module.ATTRIBUTION:
        if not entry.endswith("/"):
            assert sweep_module.classify(entry) == entry


def test_guards_are_not_counted_as_residue() -> None:
    """A file whose job is to notice a marker has to contain one.

    Counting the scanner and the tests that assert markers are absent made the
    criterion include its own measuring apparatus, so it could not reach zero
    however much was fixed — the same defect as counting attribution.
    """
    assert not set(sweep_module.GUARDS) & set(sweep_module.ALLOWLIST)
    assert not set(sweep_module.GUARDS) & set(sweep_module.ATTRIBUTION)
    for entry, reason in sweep_module.GUARDS.items():
        assert reason.strip(), f"{entry} is exempt forever without saying why"
        assert (REPO / entry).exists(), f"{entry} is exempt but is gone"
