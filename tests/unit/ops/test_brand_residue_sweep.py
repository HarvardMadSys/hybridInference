"""Tests for the brand-residue acceptance sweep."""

from __future__ import annotations

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
