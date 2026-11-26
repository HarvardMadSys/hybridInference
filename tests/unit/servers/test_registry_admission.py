"""Registry tests for the route-level ``admission`` declaration.

The field says "a Nimbus router in admission-only mode answers at this address,
in the engine's place", which is a fact about one address. A model with such a
route always has cloud routes beside it, served by the same adapter class, so the
declaration must stay on the route that made it: inherited by a cloud route, it
would have that provider's 409s read as declines.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from routing.routers import FixedRouter
from serving.servers.registry import RegistryConfigError, register_from_models_yaml

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
        base_url: http://local-gpu:8000/v1
        api_keys:
          - ${LOCAL_API_KEY}
        admission: nimbus
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
    router = FixedRouter()
    register_from_models_yaml(router, path)
    return [adapter.config for adapter, _weight in router.routes["ds-test"].adapters]


def test_the_declaration_applies_to_its_route_only(tmp_path, monkeypatch):
    local, remote = _load(tmp_path, monkeypatch, _LOCAL_PLUS_REMOTE)

    assert local.admission == "nimbus"
    assert remote.admission is None


def test_no_route_declares_it_by_default(tmp_path, monkeypatch):
    body = _LOCAL_PLUS_REMOTE.replace("        admission: nimbus\n", "")

    for cfg in _load(tmp_path, monkeypatch, body):
        assert cfg.admission is None


def test_a_model_cannot_declare_it_for_all_its_routes(tmp_path, monkeypatch):
    body = _LOCAL_PLUS_REMOTE.replace(
        "    base_url: https://api.deepseek.com\n    route:",
        "    base_url: https://api.deepseek.com\n    admission: nimbus\n    route:",
    ).replace("        admission: nimbus\n", "")

    with pytest.raises(RegistryConfigError, match="admission"):
        _load(tmp_path, monkeypatch, body)


@pytest.mark.parametrize("value", ["true", "Nimbus", "greedy", "1"])
def test_a_value_other_than_nimbus_is_refused(tmp_path, monkeypatch, value):
    body = _LOCAL_PLUS_REMOTE.replace("admission: nimbus", f"admission: {value}")

    with pytest.raises(RegistryConfigError, match="admission"):
        _load(tmp_path, monkeypatch, body)
