"""Tests for the models/routing config path settings and their env aliases.

The canonical env names are ``MODELS_CONFIG_PATH`` / ``ROUTING_CONFIG_PATH``;
the legacy ``MODELS_CONFIG`` / ``ROUTING_CONFIG`` names (used by existing
deployments and test fixtures) must keep working, with the canonical name
winning when both are set.
"""

from serving.config.settings import get_settings


def _clear(monkeypatch, *names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_models_config_path_defaults_to_unset(monkeypatch):
    _clear(monkeypatch, "MODELS_CONFIG_PATH", "MODELS_CONFIG")
    get_settings.cache_clear()
    assert get_settings().models_config_path == ""


def test_routing_config_path_defaults_to_unset(monkeypatch):
    _clear(monkeypatch, "ROUTING_CONFIG_PATH", "ROUTING_CONFIG")
    get_settings.cache_clear()
    assert get_settings().routing_config_path == ""


def test_models_config_path_canonical_name(monkeypatch):
    _clear(monkeypatch, "MODELS_CONFIG")
    monkeypatch.setenv("MODELS_CONFIG_PATH", "distributions/fi/config/models.yaml")
    get_settings.cache_clear()
    assert get_settings().models_config_path == "distributions/fi/config/models.yaml"


def test_models_config_path_legacy_name(monkeypatch):
    _clear(monkeypatch, "MODELS_CONFIG_PATH")
    monkeypatch.setenv("MODELS_CONFIG", "tests/fixtures/test_models.yaml")
    get_settings.cache_clear()
    assert get_settings().models_config_path == "tests/fixtures/test_models.yaml"


def test_models_config_path_canonical_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("MODELS_CONFIG_PATH", "canonical/models.yaml")
    monkeypatch.setenv("MODELS_CONFIG", "legacy/models.yaml")
    get_settings.cache_clear()
    assert get_settings().models_config_path == "canonical/models.yaml"


def test_routing_config_path_legacy_name(monkeypatch):
    _clear(monkeypatch, "ROUTING_CONFIG_PATH")
    monkeypatch.setenv("ROUTING_CONFIG", "tests/fixtures/test_routing.yaml")
    get_settings.cache_clear()
    assert get_settings().routing_config_path == "tests/fixtures/test_routing.yaml"


def test_routing_config_path_canonical_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("ROUTING_CONFIG_PATH", "canonical/routing.yaml")
    monkeypatch.setenv("ROUTING_CONFIG", "legacy/routing.yaml")
    get_settings.cache_clear()
    assert get_settings().routing_config_path == "canonical/routing.yaml"
