"""Tests for the distribution manifest loader and config-path precedence.

Precedence contract (design doc, Phase 1): explicit env var > manifest
``paths:`` > legacy ``config/*.yaml`` default. Unset manifest = pure legacy
behavior; a broken manifest fails open to legacy resolution; dark mode loads
the manifest but keeps legacy resolution effective.
"""

from pathlib import Path

import pytest

from serving.config.distribution import (
    DistributionConfigError,
    get_distribution_config,
    load_distribution_config,
    resolve_config_path,
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


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_distribution_config.cache_clear()


def _write_manifest(tmp_path: Path, content: str = MANIFEST) -> Path:
    path = tmp_path / "distribution.yaml"
    path.write_text(content)
    return path


def test_loader_resolves_relative_paths_against_manifest_dir(tmp_path):
    manifest = _write_manifest(tmp_path)
    config = load_distribution_config(manifest)
    assert config.distribution.id == "testdist"
    assert config.paths.models == str((tmp_path / "config/models.yaml").resolve())
    assert config.paths.routing == "/abs/routing.yaml"
    assert config.paths.alerts == ""


def test_loader_rejects_unsupported_schema_version(tmp_path):
    manifest = _write_manifest(tmp_path, "schema_version: 2\ndistribution:\n  id: x\n")
    with pytest.raises(DistributionConfigError):
        load_distribution_config(manifest)


def test_loader_rejects_malformed_yaml(tmp_path):
    manifest = _write_manifest(tmp_path, "just a string")
    with pytest.raises(DistributionConfigError):
        load_distribution_config(manifest)


def test_unset_manifest_means_legacy_resolution():
    resolved = resolve_config_path("models")
    assert resolved.source == "default"
    assert resolved.path == Path("config/models.yaml")


def test_broken_manifest_fails_open_to_legacy(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path, "schema_version: 99\ndistribution:\n  id: x\n")
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    assert get_distribution_config() is None
    assert resolve_config_path("models").source == "default"


def test_manifest_path_used_when_env_unset(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    resolved = resolve_config_path("models")
    assert resolved.source == "distribution"
    assert resolved.path == (tmp_path / "config/models.yaml").resolve()


def test_env_var_wins_over_manifest(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("MODELS_CONFIG_PATH", "env/models.yaml")
    resolved = resolve_config_path("models")
    assert resolved.source == "env"
    assert resolved.path == Path("env/models.yaml")


def test_manifest_without_entry_falls_back_to_default(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    resolved = resolve_config_path("alerts")
    assert resolved.source == "default"
    assert resolved.path == Path("config/alerts.yaml")


def test_alerts_env_counts_only_when_non_default(monkeypatch, tmp_path):
    manifest = _write_manifest(
        tmp_path,
        MANIFEST + "  alerts: ./config/alerts-dist.yaml\n",
    )
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    # Default value in env behaves as "not explicitly set": manifest wins.
    monkeypatch.setenv("ALERTS_CONFIG_PATH", "config/alerts.yaml")
    assert resolve_config_path("alerts").source == "distribution"

    get_settings.cache_clear()
    monkeypatch.setenv("ALERTS_CONFIG_PATH", "custom/alerts.yaml")
    resolved = resolve_config_path("alerts")
    assert resolved.source == "env"
    assert resolved.path == Path("custom/alerts.yaml")


def test_dark_mode_keeps_legacy_paths(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "dark")
    resolved = resolve_config_path("models")
    assert resolved.source == "default"
    assert resolved.path == Path("config/models.yaml")
    # The manifest itself still loads and validates in dark mode.
    assert get_distribution_config() is not None
