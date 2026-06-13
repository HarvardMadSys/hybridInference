"""Regression tests for serving.servers.registry — extra_body normalization.

A model with no `extra_body` (model-level or route-level) used to inherit a
`None` from top_cfg that survived into `ModelConfig(extra_body=None)`. Because
`@dataclass` `default_factory` only fires when the argument is omitted, the
explicit `None` was stored verbatim and later blew up the OpenAI-compatible
request builder at `payload = {**self.config.extra_body, ...}` with
``TypeError: 'NoneType' object is not a mapping``. That crashed every request
for the affected model, tripping all its provider circuits into a 503.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from routing.executor import RouteExecutor
from serving.adapters.base import ModelConfig
from serving.servers.registry import register_from_models_yaml

if TYPE_CHECKING:
    from pathlib import Path


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def _load_single_adapter_cfg(tmp_path: Path, monkeypatch):
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
    return router.routes["minimax-test"].adapters[0][0].config


def test_missing_extra_body_normalized_to_empty_dict(tmp_path, monkeypatch):
    cfg = _load_single_adapter_cfg(tmp_path, monkeypatch)
    assert cfg.extra_body == {}
    assert cfg.extra_body is not None


def test_missing_extra_body_does_not_break_spread(tmp_path, monkeypatch):
    cfg = _load_single_adapter_cfg(tmp_path, monkeypatch)
    # The exact operation that raised at request time: spread-unpacking the
    # config's extra_body into the upstream payload.
    payload = {**cfg.extra_body, "model": "MiniMax-M2.5"}
    assert payload["model"] == "MiniMax-M2.5"


def test_model_config_post_init_coerces_none_dict_fields():
    """__post_init__ guards every caller, not just the YAML loader."""
    cfg = ModelConfig(
        id="x",
        name="x",
        provider="minimax",
        base_url="http://x",
        extra_body=None,
        extra_headers=None,
        extra_query=None,
        route_metadata=None,
    )
    assert cfg.extra_body == {}
    assert cfg.extra_headers == {}
    assert cfg.extra_query == {}
    assert cfg.route_metadata == {}
    # The crash site, exercised directly.
    assert {**cfg.extra_body, "model": "m"} == {"model": "m"}


def test_route_level_extra_body_still_applied(tmp_path, monkeypatch):
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
                extra_body:
                  chat_template_kwargs:
                    enable_thinking: false
        """,
    )

    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)
    cfg = router.routes["minimax-test"].adapters[0][0].config
    assert cfg.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
