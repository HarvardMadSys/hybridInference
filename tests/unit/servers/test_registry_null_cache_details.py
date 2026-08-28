"""Registry tests for the route-level ``null_cache_details_means_miss`` flag.

The flag says "a null ``*_tokens_details`` block from this endpoint is a
reported cache miss, not silence" -- a fact about how one server was started
(sglang's ``--enable-cache-report``), not about the model. Every local model
here also carries a remote fallback through the same adapter class, so the
per-route resolution is what keeps a fabricated 0 off an endpoint that reports
nothing at all.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

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
        null_cache_details_means_miss: true
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

    assert local.null_cache_details_means_miss is True
    assert remote.null_cache_details_means_miss is False


def test_default_is_off_for_every_route(tmp_path, monkeypatch):
    # A model must not start recording fabricated misses on its remote routes
    # just because it gained a local one that does report.
    body = _LOCAL_PLUS_REMOTE.replace("        null_cache_details_means_miss: true\n", "")

    for cfg in _load(tmp_path, monkeypatch, body):
        assert cfg.null_cache_details_means_miss is False


def test_model_level_declaration_is_refused(tmp_path, monkeypatch):
    """The claim is about one server, so it must not be spelled model-wide.

    Inheritance would carry it to every fallback the model has -- and to a
    shorthand model's synthesized route, which has no `route:` block for a
    reviewer to look at.
    """
    body = """
    models:
      - id: ds-test
        name: ds-test
        provider: sglang
        base_url: http://local-gpu:8003/v1
        null_cache_details_means_miss: true
        route:
          - kind: sglang
            weight: 1.0
            base_url: http://local-gpu:8003/v1
            api_keys:
              - ${LOCAL_API_KEY}
    """

    with pytest.raises(ValueError, match="must be declared on a route"):
        _load(tmp_path, monkeypatch, body)


def test_shorthand_model_cannot_declare_it(tmp_path, monkeypatch):
    """A model with no `route:` block gets a synthesized route; it must not inherit.

    This is the bypass a config-file guard that only walks explicit `route:`
    lists would never see, so the loader has to refuse it.
    """
    body = """
    models:
      - id: ds-test
        name: ds-test
        provider: sglang
        base_url: http://local-gpu:8003/v1
        api_key: ${LOCAL_API_KEY}
        null_cache_details_means_miss: true
    """

    with pytest.raises(ValueError, match="must be declared on a route"):
        _load(tmp_path, monkeypatch, body)


@pytest.mark.parametrize("scalar", ['"false"', '"true"', '"no"', '"on"', "1", "0"])
def test_non_boolean_scalar_is_refused(tmp_path, monkeypatch, scalar):
    """`bool("false")` is True -- a quoted opt-out must not become an opt-in.

    Unquoted ``yes``/``no``/``on``/``off`` are excluded: PyYAML (YAML 1.1)
    resolves those to real booleans, so they arrive already typed and are
    accepted like ``true``/``false``. Quoted, they are strings and refused.
    """
    body = f"""
    models:
      - id: ds-test
        name: ds-test
        provider: sglang
        base_url: http://local-gpu:8003/v1
        route:
          - kind: sglang
            weight: 1.0
            base_url: http://local-gpu:8003/v1
            api_keys:
              - ${{LOCAL_API_KEY}}
            null_cache_details_means_miss: {scalar}
    """

    with pytest.raises(ValueError, match="must be a YAML boolean"):
        _load(tmp_path, monkeypatch, body)


def test_yaml_false_is_accepted_and_off(tmp_path, monkeypatch):
    body = _LOCAL_PLUS_REMOTE.replace(
        "        null_cache_details_means_miss: true\n",
        "        null_cache_details_means_miss: false\n",
    )

    local, remote = _load(tmp_path, monkeypatch, body)

    assert local.null_cache_details_means_miss is False
    assert remote.null_cache_details_means_miss is False


# Both routes go through OpenAICompatAdapter and the default usage profile, so
# the only thing separating them is the declaration itself. This is the shape
# the deployment actually has: an sglang node that reports beside a DGX Spark
# vLLM node that sends the same null whether or not the prefix cache hit.
_SGLANG_PLUS_VLLM = """
models:
  - id: ds-test
    name: ds-test
    provider: sglang
    base_url: http://local-gpu:8003/v1
    route:
      - kind: sglang
        weight: 1.0
        base_url: http://local-gpu:8003/v1
        api_keys:
          - ${LOCAL_API_KEY}
        null_cache_details_means_miss: true
      - kind: vllm
        weight: 1.0
        base_url: http://spark:8002/v1
        api_keys:
          - ${LOCAL_API_KEY}
"""


def test_flag_reaches_the_adapter_usage_normalizer(tmp_path, monkeypatch):
    """The declaration has to change what the adapter records, not just sit on config.

    Verbatim sglang usage for a cold prompt: the licensed route reads the null
    details block as a measured 0 (``cache_read_reported``), the unlicensed one
    leaves it unknown so ``api_logs.cache_read_tokens`` stays NULL.
    """
    monkeypatch.setenv("LOCAL_API_KEY", "local-key")
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(_SGLANG_PLUS_VLLM))
    router = RouteExecutor()
    register_from_models_yaml(router, path)
    local_adapter, remote_adapter = (a for a, _w in router.routes["ds-test"].adapters)

    cold = {
        "prompt_tokens": 4817,
        "total_tokens": 4818,
        "completion_tokens": 1,
        "prompt_tokens_details": None,
        "reasoning_tokens": 0,
    }

    licensed = local_adapter._parse_usage(cold)
    assert licensed.cache_read_reported is True
    assert licensed.cache_read_tokens == 0
    assert licensed.to_dict()["cache_read_tokens"] == 0

    unlicensed = remote_adapter._parse_usage(cold)
    assert unlicensed.cache_read_reported is False
    assert "cache_read_tokens" not in unlicensed.to_dict()
