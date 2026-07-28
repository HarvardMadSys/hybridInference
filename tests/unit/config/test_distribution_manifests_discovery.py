"""Neutral CI gate: every checked-in distribution manifest must validate.

The per-distribution invariant tests live with their overlay
(``distributions/<dist>/tests/``), which local ``make test`` collects via
pytest ``testpaths`` — but the CI sharder currently only walks ``tests/``.
This discovery test lives in ``tests/`` precisely so manifest validation
runs in PR CI without touching the frozen workflows. The second test is a
temporary FreeInference Phase 1 compatibility gate: until Phase 2 deliberately
moves production truth, the overlay paths must resolve to the legacy files.
"""

from pathlib import Path

from serving.config.distribution import load_distribution_config

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_every_checked_in_distribution_manifest_loads():
    manifests = sorted(REPO_ROOT.glob("distributions/*/distribution.yaml"))
    for manifest in manifests:
        config = load_distribution_config(manifest)
        assert config.distribution.id, f"{manifest}: distribution.id must be non-empty"
        for kind in ("models", "routing", "alerts"):
            value = getattr(config.paths, kind)
            if value:
                assert Path(value).exists(), f"{manifest}: paths.{kind} -> {value} missing"


def test_freeinference_overlay_preserves_phase_one_legacy_aliases():
    """Keep the stack's explicit Phase 1 compatibility invariant in PR CI."""
    manifest = REPO_ROOT / "distributions" / "freeinference" / "distribution.yaml"
    config = load_distribution_config(manifest)
    # alerts.yaml has moved into the overlay; models and routing have not.
    # Each line here is a claim about where production reads from, so moving
    # one without editing this is the mistake worth catching.
    # models.yaml is the last one still aliasing legacy truth — it changes
    # daily, so it moves when the drift window costs least. The other two are
    # in the overlay, with nothing left at the old path: two copies of a
    # deployment's config is how they drift apart.
    # All three now live in the overlay; config/ holds none of them, which is
    # the point — a second copy at the legacy path is how the two drift apart.
    overlay_config = REPO_ROOT / "distributions" / "freeinference" / "config"
    for kind in ("models", "alerts", "routing"):
        resolved = Path(getattr(config.paths, kind))
        assert resolved == (overlay_config / f"{kind}.yaml").resolve(), (
            f"{kind}.yaml moved into the overlay; the manifest must follow"
        )
        assert not (REPO_ROOT / "config" / f"{kind}.yaml").exists(), (
            f"a second copy of {kind}.yaml at the legacy path will drift"
        )
