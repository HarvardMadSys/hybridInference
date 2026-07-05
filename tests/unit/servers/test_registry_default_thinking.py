"""serving.servers.registry propagates model-level ``default_thinking``.

``default_thinking`` lets a model default reasoning off (e.g.
``{"type": "disabled"}``) when the client sends neither ``thinking`` nor
``reasoning_effort``. It is declared once at the model level and must reach
every route's ``ModelConfig`` so the request handler can source it regardless
of which endpoint the router selects.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from routing.executor import RouteExecutor
from serving.servers.registry import register_from_models_yaml

if TYPE_CHECKING:
    from pathlib import Path


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def test_default_thinking_propagates_to_all_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "key-one")
    monkeypatch.setenv("OLLAMA_API_KEY", "key-two")
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: minimax-test
            name: minimax-test
            provider: minimax
            base_url: https://api.minimax.io
            supported_params: [temperature, thinking]
            default_thinking: {type: disabled}
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
        """,
    )

    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)

    adapters = router.routes["minimax-test"].adapters
    assert len(adapters) == 2
    # Declared once on the model, inherited by every route's config.
    for adapter, _weight in adapters:
        assert adapter.config.default_thinking == {"type": "disabled"}


def test_default_thinking_absent_defaults_to_none(tmp_path, monkeypatch):
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

    cfg = router.routes["minimax-test"].adapters[0][0].config
    assert cfg.default_thinking is None
