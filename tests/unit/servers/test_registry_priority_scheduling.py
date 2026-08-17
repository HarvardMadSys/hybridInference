"""Registry tests for the route-level ``priority_scheduling`` declaration.

The flag says "this endpoint's server was started with sglang's
``--enable-priority-scheduling``", which is a fact about one server rather than
about the model. Every local model here also carries a remote fallback through
the same adapter class, so the per-route resolution is what keeps the field off
an API that never agreed to accept it.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from routing.executor import RouteExecutor
from serving.servers.registry import register_from_models_yaml

if TYPE_CHECKING:
    from pathlib import Path


_LOCAL_PLUS_REMOTE = """
models:
  - id: ds-test
    name: ds-test
    provider: deepseek
    base_url: https://api.deepseek.com
    route:
      - kind: sglang
        weight: 1.0
        base_url: http://local-gpu:8003/v1
        api_keys:
          - ${LOCAL_API_KEY}
        priority_scheduling: true
      - kind: deepseek
        weight: 1.0
        base_url: https://api.deepseek.com
        api_keys:
          - ${DEEPSEEK_API_KEY}
"""


def _load(tmp_path: Path, monkeypatch, body: str) -> list:
    monkeypatch.setenv("LOCAL_API_KEY", "local-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "remote-key")
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body))
    router = RouteExecutor()
    register_from_models_yaml(router, path)
    return [adapter.config for adapter, _weight in router.routes["ds-test"].adapters]


def test_route_level_flag_applies_to_that_route_only(tmp_path, monkeypatch):
    local, remote = _load(tmp_path, monkeypatch, _LOCAL_PLUS_REMOTE)

    assert local.priority_scheduling is True
    assert remote.priority_scheduling is False


def test_default_is_off_for_every_route(tmp_path, monkeypatch):
    # Nothing may start sending a scheduling field to upstreams that never asked
    # for it just because a model gained a local route.
    body = _LOCAL_PLUS_REMOTE.replace("        priority_scheduling: true\n", "")

    for cfg in _load(tmp_path, monkeypatch, body):
        assert cfg.priority_scheduling is False


def test_model_level_flag_is_inherited_and_coerced(tmp_path, monkeypatch):
    # A single-server model can declare it once at the top rather than repeating
    # it per route; YAML truthiness is coerced so "yes"/1 cannot reach the
    # adapter as a non-bool.
    body = """
    models:
      - id: ds-test
        name: ds-test
        provider: sglang
        base_url: http://local-gpu:8003/v1
        priority_scheduling: yes
        route:
          - kind: sglang
            weight: 1.0
            base_url: http://local-gpu:8003/v1
            api_keys:
              - ${LOCAL_API_KEY}
    """

    (cfg,) = _load(tmp_path, monkeypatch, body)

    assert cfg.priority_scheduling is True


def test_route_can_opt_out_of_an_inherited_flag(tmp_path, monkeypatch):
    # A model-level declaration must not follow a fallback route to a provider
    # that would reject the field.
    body = """
    models:
      - id: ds-test
        name: ds-test
        provider: sglang
        base_url: http://local-gpu:8003/v1
        priority_scheduling: true
        route:
          - kind: sglang
            weight: 1.0
            base_url: http://local-gpu:8003/v1
            api_keys:
              - ${LOCAL_API_KEY}
          - kind: deepseek
            weight: 1.0
            base_url: https://api.deepseek.com
            api_keys:
              - ${DEEPSEEK_API_KEY}
            priority_scheduling: false
    """

    local, remote = _load(tmp_path, monkeypatch, body)

    assert local.priority_scheduling is True
    assert remote.priority_scheduling is False
