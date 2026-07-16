"""Bootstrap integration for the distribution manifest path precedence."""

from __future__ import annotations

import pytest

from routing.executor import RouteExecutor
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
