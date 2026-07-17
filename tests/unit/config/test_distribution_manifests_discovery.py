"""Neutral CI gate: every checked-in distribution manifest must validate.

The per-distribution invariant tests live with their overlay
(``distributions/<dist>/tests/``), which local ``make test`` collects via
pytest ``testpaths`` — but the CI sharder currently only walks ``tests/``.
This discovery test lives in ``tests/`` precisely so manifest validation
runs in PR CI without touching the frozen workflows; it stays neutral by
asserting nothing distribution-specific.
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
