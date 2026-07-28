"""Tests for the brand-residue acceptance sweep."""

from __future__ import annotations

from ops.admin import brand_residue_sweep as sweep_module
from ops.admin.brand_residue_sweep import classify, file_has_marker


def test_classify_matches_prefixes_and_exact_files() -> None:
    assert classify("distributions/freeinference/distribution.yaml") == "distributions/"
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

    LICENSE names the copyright holder; pyproject.toml and uv.lock name the
    org a public dependency is fetched from; the READMEs name the deployment
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
