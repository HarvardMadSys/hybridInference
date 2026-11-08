"""Tests for the distribution manifest loader and config-path precedence.

Precedence contract (design doc, Phase 1): explicit env var > manifest
``paths:`` > legacy ``config/*.yaml`` default. Unset manifest = pure legacy
behavior; a broken manifest fails open to legacy resolution; dark mode loads
the manifest and compares it against whatever is actually effective (env or
legacy) without changing resolution; invalid modes degrade to dark, never to
active.
"""

import os
from pathlib import Path

import pytest

from serving.config import distribution
from serving.config.distribution import (
    DistributionConfigError,
    get_distribution_config,
    load_distribution_config,
)
from serving.config.settings import get_settings

_ENV_VARS = (
    "DISTRIBUTION_CONFIG_PATH",
    "DISTRIBUTION_CONFIG_MODE",
    "MODELS_CONFIG_PATH",
    "MODELS_CONFIG",
    "ROUTING_CONFIG_PATH",
    "ROUTING_CONFIG",
    "ALERTS_CONFIG_PATH",
)

MANIFEST = """\
schema_version: 1
distribution:
  id: testdist
  release: 2026.07.1
paths:
  models: ./config/models.yaml
  routing: /abs/routing.yaml
"""

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BRANDING_EXAMPLE = _REPO_ROOT / "config" / "examples" / "branding.example.json"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Case-insensitive purge: Settings matches env vars case-insensitively,
    # so a stray lowercase `distribution_config_mode=active` in the runner
    # environment would otherwise leak into these tests.
    targets = {var.casefold() for var in _ENV_VARS}
    for key in list(os.environ):
        if key.casefold() in targets:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    distribution._logged_once.clear()
    yield
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    distribution._logged_once.clear()


def _write_manifest(tmp_path: Path, content: str = MANIFEST) -> Path:
    path = tmp_path / "distribution.yaml"
    path.write_text(content)
    return path


# --- Loader ---


def test_loader_resolves_relative_paths_against_manifest_dir(tmp_path):
    manifest = _write_manifest(tmp_path)
    config = load_distribution_config(manifest)
    assert config.distribution.id == "testdist"
    assert config.paths.models == str((tmp_path / "config/models.yaml").resolve())
    assert config.paths.routing == "/abs/routing.yaml"
    assert config.paths.alerts == ""


def test_loader_resolves_and_validates_relative_branding_path(tmp_path):
    branding = tmp_path / "branding" / "site.yaml"
    branding.parent.mkdir()
    branding.write_text(_BRANDING_EXAMPLE.read_text())
    manifest = _write_manifest(
        tmp_path,
        MANIFEST.replace("paths:\n", "site:\n  branding: ./branding/site.yaml\npaths:\n"),
    )

    config = load_distribution_config(manifest)

    assert config.site.branding == str(branding.resolve())
    assert config.branding_config is not None
    assert config.branding_config.organization.name == "Example Organization"


def test_loader_rejects_unsupported_schema_version(tmp_path):
    manifest = _write_manifest(tmp_path, "schema_version: 2\ndistribution:\n  id: x\n")
    with pytest.raises(DistributionConfigError):
        load_distribution_config(manifest)


def test_loader_rejects_malformed_yaml(tmp_path):
    manifest = _write_manifest(tmp_path, "just a string")
    with pytest.raises(DistributionConfigError):
        load_distribution_config(manifest)


def test_loader_rejects_undecodable_bytes(tmp_path):
    path = tmp_path / "distribution.yaml"
    path.write_bytes(b"\xff\xfe\xfa schema_version: 1")
    with pytest.raises(DistributionConfigError):
        load_distribution_config(path)


def test_loader_rejects_unresolvable_path_values(tmp_path):
    manifest = _write_manifest(
        tmp_path,
        'schema_version: 1\ndistribution:\n  id: x\npaths:\n  models: "a\\0b"\n',
    )
    with pytest.raises(DistributionConfigError):
        load_distribution_config(manifest)


# --- Fail-open ---


# --- Precedence ---


# --- Modes ---
