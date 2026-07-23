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


def test_loader_rejects_unsupported_schema_version(tmp_path):
    manifest = _write_manifest(tmp_path, "schema_version: 3\ndistribution:\n  id: x\n")
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


def test_broken_manifest_fails_open_to_legacy(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path, "schema_version: 99\ndistribution:\n  id: x\n")
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    assert get_distribution_config() is None
    assert resolve_config_path("models").source == "default"


def test_undecodable_manifest_fails_open_to_legacy(monkeypatch, tmp_path):
    path = tmp_path / "distribution.yaml"
    path.write_bytes(b"\xff\xfe\xfa schema_version: 1")
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(path))
    assert get_distribution_config() is None
    assert resolve_config_path("models").source == "default"


def test_nul_byte_path_fails_open_to_legacy(monkeypatch, tmp_path):
    manifest = _write_manifest(
        tmp_path,
        'schema_version: 1\ndistribution:\n  id: x\npaths:\n  models: "a\\0b"\n',
    )
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    assert get_distribution_config() is None
    assert resolve_config_path("models").source == "default"


@pytest.mark.parametrize("mode", ["active", "dark"])
def test_absolute_nul_byte_path_fails_open_in_both_modes(monkeypatch, tmp_path, mode):
    """Absolute paths are validated at load time too, not only relative ones."""
    manifest = _write_manifest(
        tmp_path,
        'schema_version: 1\ndistribution:\n  id: x\npaths:\n  models: "/a\\0b"\n',
    )
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", mode)
    assert get_distribution_config() is None
    resolved = resolve_config_path("models")
    assert resolved.source == "default"
    assert resolved.path == Path("config/models.yaml")


# --- Precedence ---


def test_unset_manifest_means_legacy_resolution():
    resolved = resolve_config_path("models")
    assert resolved.source == "default"
    assert resolved.path == Path("config/models.yaml")


def test_manifest_path_used_when_env_unset(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    resolved = resolve_config_path("models")
    assert resolved.source == "distribution"
    assert resolved.path == (tmp_path / "config/models.yaml").resolve()


def test_env_var_wins_over_manifest(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    monkeypatch.setenv("MODELS_CONFIG_PATH", "env/models.yaml")
    resolved = resolve_config_path("models")
    assert resolved.source == "env"
    assert resolved.path == Path("env/models.yaml")
    # The manifest is still loaded and validated despite being shadowed.
    assert get_distribution_config() is not None


def test_manifest_without_entry_falls_back_to_default(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    resolved = resolve_config_path("alerts")
    assert resolved.source == "default"
    assert resolved.path == Path("config/alerts.yaml")


def test_alerts_explicit_env_wins_even_at_default_value(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path, MANIFEST + "  alerts: ./config/alerts-dist.yaml\n")
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    # Explicitly present in the environment counts as an override even when
    # the value equals the legacy default: env > manifest.
    monkeypatch.setenv("ALERTS_CONFIG_PATH", "config/alerts.yaml")
    resolved = resolve_config_path("alerts")
    assert resolved.source == "env"
    assert resolved.path == Path("config/alerts.yaml")

    monkeypatch.delenv("ALERTS_CONFIG_PATH")
    get_settings.cache_clear()
    assert resolve_config_path("alerts").source == "distribution"


# --- Modes ---


def test_dark_mode_keeps_legacy_paths(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "dark")
    resolved = resolve_config_path("models")
    assert resolved.source == "default"
    assert resolved.path == Path("config/models.yaml")
    # The manifest itself still loads and validates in dark mode.
    assert get_distribution_config() is not None


def test_dark_mode_compares_against_env_override(monkeypatch, tmp_path, caplog):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "dark")
    monkeypatch.setenv("MODELS_CONFIG_PATH", "env/models.yaml")
    with caplog.at_level("INFO"):
        resolved = resolve_config_path("models")
    assert resolved.source == "env"
    assert get_distribution_config() is not None
    assert "[distribution dark mode]" in caplog.text


def test_invalid_mode_degrades_to_dark_not_active(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "drak")
    resolved = resolve_config_path("models")
    assert resolved.source == "default"


def test_mode_is_case_insensitive(monkeypatch, tmp_path):
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "DARK")
    assert resolve_config_path("models").source == "default"

    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "Active")
    get_settings.cache_clear()
    assert resolve_config_path("models").source == "distribution"


def test_default_mode_is_dark(monkeypatch, tmp_path):
    """Setting only the path can never change behavior: dark by default."""
    manifest = _write_manifest(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    resolved = resolve_config_path("models")
    assert resolved.source == "default"
    assert get_distribution_config() is not None


def test_alerts_lowercase_env_var_counts_as_explicit(monkeypatch, tmp_path):
    """Settings is case-insensitive; the presence check must be too."""
    manifest = _write_manifest(tmp_path, MANIFEST + "  alerts: ./config/alerts-dist.yaml\n")
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    monkeypatch.setenv("alerts_config_path", "custom/alerts.yaml")
    resolved = resolve_config_path("alerts")
    assert resolved.source == "env"
    assert resolved.path == Path("custom/alerts.yaml")


def test_constructor_supplied_alerts_counts_as_explicit(monkeypatch, tmp_path):
    """model_fields_set covers non-env sources (.env, constructor) too."""
    from serving.config import distribution
    from serving.config.settings import Settings

    manifest = _write_manifest(tmp_path, MANIFEST + "  alerts: ./config/alerts-dist.yaml\n")
    custom = Settings(
        _env_file=None,
        distribution_config_path=str(manifest),
        distribution_config_mode="active",
        alerts_config_path="custom/alerts.yaml",
    )
    monkeypatch.setattr(distribution, "get_settings", lambda: custom)
    get_distribution_config.cache_clear()
    resolved = resolve_config_path("alerts")
    assert resolved.source == "env"
    assert resolved.path == Path("custom/alerts.yaml")
