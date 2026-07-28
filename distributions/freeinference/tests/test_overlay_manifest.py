"""Validation for the checked-in FreeInference distribution manifest.

Seed of the distribution-CI "manifest validation" gate: the real overlay
manifest must always load, and while Phase 1 holds, its paths must point at
the legacy config files so dark mode compares as identical.
"""

from pathlib import Path

from serving.config.distribution import load_distribution_config

OVERLAY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OVERLAY_ROOT.parents[1]
MANIFEST = OVERLAY_ROOT / "distribution.yaml"


def test_manifest_loads_and_identifies_freeinference():
    config = load_distribution_config(MANIFEST)
    assert config.schema_version == 1
    assert config.distribution.id == "freeinference"
    assert config.site.public_base_url == "https://freeinference.org"
    assert set(config.features.routers) == {"fixed", "routewise"}


def test_manifest_paths_point_at_legacy_truth():
    """Phase 1 invariant: overlay paths alias the legacy config files."""
    config = load_distribution_config(MANIFEST)
    # alerts.yaml has moved into the overlay; models and routing have not.
    # Each line here is a claim about where production reads from, so moving
    # one without editing this is the mistake worth catching.
    # models.yaml is the last one still aliasing legacy truth — it changes
    # daily, so it moves when the drift window costs least. The other two are
    # in the overlay, with nothing left at the old path: two copies of a
    # deployment's config is how they drift apart.
    models = Path(config.paths.models)
    assert models == (REPO_ROOT / "config" / "models.yaml").resolve(), (
        "models.yaml no longer aliases legacy truth"
    )

    overlay_config = REPO_ROOT / "distributions" / "freeinference" / "config"
    for kind in ("alerts", "routing"):
        resolved = Path(getattr(config.paths, kind))
        assert resolved == (overlay_config / f"{kind}.yaml").resolve(), (
            f"{kind}.yaml moved into the overlay; the manifest must follow"
        )
        assert not (REPO_ROOT / "config" / f"{kind}.yaml").exists(), (
            f"a second copy of {kind}.yaml at the legacy path will drift"
        )
        assert resolved.exists()
