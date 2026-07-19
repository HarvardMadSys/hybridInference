"""Tests for the models/routing config path settings and their env aliases.

The canonical env names are ``MODELS_CONFIG_PATH`` / ``ROUTING_CONFIG_PATH``;
the legacy ``MODELS_CONFIG`` / ``ROUTING_CONFIG`` names (used by existing
deployments and test fixtures) must keep working, with the canonical name
winning when both are set.

``Settings`` is built with ``_env_file=None`` so a developer's local ``.env``
cannot leak into the assertions.
"""

from serving.config.settings import Settings

_ALL_NAMES = ("MODELS_CONFIG_PATH", "MODELS_CONFIG", "ROUTING_CONFIG_PATH", "ROUTING_CONFIG")


def _settings(monkeypatch, **env: str) -> Settings:
    for name in _ALL_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_models_config_path_defaults_to_unset(monkeypatch):
    assert _settings(monkeypatch).models_config_path == ""


def test_routing_config_path_defaults_to_unset(monkeypatch):
    assert _settings(monkeypatch).routing_config_path == ""


def test_models_config_path_canonical_name(monkeypatch):
    settings = _settings(monkeypatch, MODELS_CONFIG_PATH="distributions/fi/config/models.yaml")
    assert settings.models_config_path == "distributions/fi/config/models.yaml"


def test_models_config_path_legacy_name(monkeypatch):
    settings = _settings(monkeypatch, MODELS_CONFIG="tests/fixtures/test_models.yaml")
    assert settings.models_config_path == "tests/fixtures/test_models.yaml"


def test_models_config_path_canonical_wins_over_legacy(monkeypatch):
    settings = _settings(
        monkeypatch,
        MODELS_CONFIG_PATH="canonical/models.yaml",
        MODELS_CONFIG="legacy/models.yaml",
    )
    assert settings.models_config_path == "canonical/models.yaml"


def test_routing_config_path_legacy_name(monkeypatch):
    settings = _settings(monkeypatch, ROUTING_CONFIG="tests/fixtures/test_routing.yaml")
    assert settings.routing_config_path == "tests/fixtures/test_routing.yaml"


def test_routing_config_path_canonical_wins_over_legacy(monkeypatch):
    settings = _settings(
        monkeypatch,
        ROUTING_CONFIG_PATH="canonical/routing.yaml",
        ROUTING_CONFIG="legacy/routing.yaml",
    )
    assert settings.routing_config_path == "canonical/routing.yaml"


def test_constructor_accepts_field_names(monkeypatch):
    for name in _ALL_NAMES:
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=None,
        models_config_path="by-name/models.yaml",
        routing_config_path="by-name/routing.yaml",
    )
    assert settings.models_config_path == "by-name/models.yaml"
    assert settings.routing_config_path == "by-name/routing.yaml"
