"""Bootstrap integration for the distribution manifest path precedence."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from routing.executor import RouteExecutor
from serving.config.distribution import DistributionStartupError, get_distribution_config
from serving.config.settings import get_settings
from serving.servers import bootstrap

MODELS_YAML = """\
models:
  - id: dist-model
    name: Dist Model
    provider: zai
    base_url: https://dist.example.test
    api_key: sk-dist
    aliases: ["dist-alias"]
"""


def _write(tmp_path, models_name: str = "dist-models.yaml"):
    models_yaml = tmp_path / models_name
    models_yaml.write_text(MODELS_YAML)
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(
        f"schema_version: 1\ndistribution:\n  id: testdist\npaths:\n  models: ./{models_name}\n"
    )
    return manifest


@pytest.mark.asyncio
async def test_manifest_models_path_registers_routes(monkeypatch, tmp_path):
    manifest = _write(tmp_path)
    for var in ("MODELS_CONFIG", "MODELS_CONFIG_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")

    router = RouteExecutor()
    _embedding, infos = await bootstrap._init_router_and_models(router)

    assert set(router.routes) == {"dist-model", "dist-alias"}
    assert [info.model_id for info in infos] == ["dist-model"]


@pytest.mark.asyncio
async def test_env_var_beats_manifest_in_bootstrap(monkeypatch, tmp_path):
    manifest = _write(tmp_path)
    env_models = tmp_path / "env-models.yaml"
    env_models.write_text(MODELS_YAML.replace("dist-model", "env-model"))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    monkeypatch.setenv("MODELS_CONFIG", str(env_models))

    router = RouteExecutor()
    await bootstrap._init_router_and_models(router)

    assert "env-model" in router.routes
    assert "dist-model" not in router.routes


@pytest.mark.asyncio
async def test_dark_mode_ignores_manifest_paths_in_bootstrap(monkeypatch, tmp_path):
    manifest = _write(tmp_path)
    for var in ("MODELS_CONFIG", "MODELS_CONFIG_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "dark")
    monkeypatch.chdir(tmp_path)  # no config/models.yaml here → legacy skip

    router = RouteExecutor()
    await bootstrap._init_router_and_models(router)

    assert "dist-model" not in router.routes


def _write_v2_required_fixture(tmp_path, *, models: str = "models.yaml"):
    (tmp_path / "environment.yaml").write_text("environment_schema_version: 1\nvariables: []\n")
    manifest = tmp_path / "distribution.v2.yaml"
    manifest.write_text(
        f"""\
schema_version: 2
distribution:
  id: testdist
resources:
  gateway:
    models: {models}
environment_contract: environment.yaml
"""
    )
    return manifest


def _configure_v2_required(monkeypatch, manifest):
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_REQUIRED", "1")
    monkeypatch.setenv("DISTRIBUTION_MODELS_MODE", "active")
    monkeypatch.setenv("DISTRIBUTION_ROUTING_MODE", "legacy")
    monkeypatch.setenv("DISTRIBUTION_ALERTS_MODE", "legacy")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()


@pytest.mark.asyncio
async def test_required_model_parser_error_escapes_bootstrap_catch(monkeypatch, tmp_path):
    (tmp_path / "models.yaml").write_text("models:\n  - invalid scalar\n")
    manifest = _write_v2_required_fixture(tmp_path)
    _configure_v2_required(monkeypatch, manifest)

    with pytest.raises(DistributionStartupError, match="models config failed"):
        await bootstrap._init_router_and_models(RouteExecutor())


@pytest.mark.asyncio
async def test_initialize_runs_distribution_preflight_before_database_setup():
    startup_error = DistributionStartupError("static failure")
    with (
        patch(
            "serving.servers.bootstrap.preflight_distribution_config",
            side_effect=startup_error,
        ) as preflight,
        patch("serving.servers.bootstrap._init_db_logger") as init_db,
        pytest.raises(DistributionStartupError, match="static failure"),
    ):
        await bootstrap.initialize()

    preflight.assert_called_once_with()
    init_db.assert_not_called()
