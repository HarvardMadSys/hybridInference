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
    # All three have moved. `config/` holds none of them, which is the point:
    # a second copy at the legacy path is how the two drift apart, and the
    # gateway would keep running on whichever one it happened to read.
    overlay_config = REPO_ROOT / "distributions" / "freeinference" / "config"
    for kind in ("models", "alerts", "routing"):
        resolved = Path(getattr(config.paths, kind))
        assert resolved == (overlay_config / f"{kind}.yaml").resolve(), (
            f"{kind}.yaml moved into the overlay; the manifest must follow"
        )
        assert not (REPO_ROOT / "config" / f"{kind}.yaml").exists(), (
            f"a second copy of {kind}.yaml at the legacy path will drift"
        )
        assert resolved.exists()
