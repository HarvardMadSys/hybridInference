"""Regression tests for serving.servers.registry — processor propagation.

A model-level ``processor:`` must reach every route's ``ModelConfig``. The
field used to be read only per route, so a model-level declaration was silently
dropped and each route fell back to model-ID auto-detection. Route-level
``processor:`` still overrides the model-level default.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from routing.executor import RouteExecutor
from serving.servers.registry import register_from_models_yaml

if TYPE_CHECKING:
    from pathlib import Path

_TWO_ROUTE_MODEL = """
models:
  - id: minimax-m3-test
    name: minimax-m3-test
    provider: minimax
    base_url: https://api.minimax.io
    processor: reasoning_extract
    route:
      - kind: minimax
        weight: 1.0
        base_url: https://api.minimax.io
        api_keys:
          - ${MINIMAX_API_KEY}
      - kind: ollama
        weight: 0.01
        base_url: https://ollama.example
        api_keys:
          - ${OLLAMA_API_KEY}
__OLLAMA_EXTRA__
"""


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def _configs(router: RouteExecutor, model_id: str):
    return [adapter.config for adapter, _weight in router.routes[model_id].adapters]


def test_model_level_processor_propagates_to_all_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "key-one")
    monkeypatch.setenv("OLLAMA_API_KEY", "key-two")
    yaml_path = _write_yaml(tmp_path, _TWO_ROUTE_MODEL.replace("__OLLAMA_EXTRA__", ""))

    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)

    configs = _configs(router, "minimax-m3-test")
    assert len(configs) == 2
    assert all(cfg.processor == "reasoning_extract" for cfg in configs)


def test_route_level_processor_overrides_model_level(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "key-one")
    monkeypatch.setenv("OLLAMA_API_KEY", "key-two")
    yaml_path = _write_yaml(
        tmp_path,
        _TWO_ROUTE_MODEL.replace("__OLLAMA_EXTRA__", "        processor: default"),
    )

    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)

    by_provider = {cfg.provider: cfg.processor for cfg in _configs(router, "minimax-m3-test")}
    assert by_provider["minimax"] == "reasoning_extract"
    assert by_provider["ollama"] == "default"


def test_absent_processor_defaults_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "key-one")
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: minimax-test
            name: minimax-test
            provider: minimax
            base_url: https://api.minimax.io
            route:
              - kind: minimax
                weight: 1.0
                base_url: https://api.minimax.io
                api_keys:
                  - ${MINIMAX_API_KEY}
        """,
    )

    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)

    (cfg,) = _configs(router, "minimax-test")
    assert cfg.processor is None
