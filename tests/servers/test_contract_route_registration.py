"""Contract freeze: model registration semantics from models.yaml.

Characterization tests for ``register_from_models_yaml`` ahead of the
config-path / distribution-overlay migration
(docs/agents/specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md).
When the production models.yaml later moves into the FreeInference overlay,
these tests must keep passing unchanged: they pin the loader's semantics,
not the production catalog's contents.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from routing.executor import RouteExecutor
from serving.servers import registry

FIXTURE_YAML = """\
models:
  - id: contract-hybrid
    name: Contract Hybrid
    provider: openai_compat
    context_length: 8192
    max_output_length: 1024
    aliases: ["contract-hybrid-alias"]
    route:
      - kind: openai_compat
        weight: 0.7
        base_url: https://remote.example.test/v1
        api_key: sk-remote
      - kind: vllm
        weight: 0.3
        base_url: http://local.example.test:8000
  - id: contract-simple
    name: Contract Simple
    provider: zai
    base_url: https://zai.example.test
    api_key: sk-zai
    context_length: 4096
    max_output_length: 512
    aliases: ["contract-alias-1", "contract-alias-2"]
"""


@pytest.fixture
def registered(tmp_path) -> tuple[RouteExecutor, int]:
    p = tmp_path / "models.yaml"
    p.write_text(FIXTURE_YAML)
    exe = RouteExecutor()
    count, _infos = registry.register_from_models_yaml(exe, Path(p))
    return exe, count


def test_registers_canonical_ids_and_all_aliases(registered):
    exe, count = registered
    # 2 canonical models + 3 aliases.
    assert count == 5
    assert set(exe.routes) == {
        "contract-hybrid",
        "contract-hybrid-alias",
        "contract-simple",
        "contract-alias-1",
        "contract-alias-2",
    }


def test_hybrid_model_keeps_both_adapters_and_weights(registered):
    exe, _ = registered
    adapters = exe.routes["contract-hybrid"].adapters
    assert [(a.config.base_url, w) for a, w in adapters] == [
        ("https://remote.example.test/v1", 0.7),
        ("http://local.example.test:8000", 0.3),
    ]


def test_aliases_share_canonical_adapters(registered):
    exe, _ = registered
    canonical = exe.routes["contract-simple"].adapters
    for alias in ("contract-alias-1", "contract-alias-2"):
        alias_adapters = exe.routes[alias].adapters
        assert [a for a, _ in alias_adapters] == [a for a, _ in canonical]


def test_endpoint_ids_are_provider_host_port_scoped(registered):
    exe, _ = registered
    for route in exe.routes.values():
        for adapter, _weight in route.adapters:
            endpoint_id = adapter.config.endpoint_id
            assert endpoint_id, "every adapter must expose a non-empty endpoint_id"
            assert ":" in endpoint_id, f"unexpected endpoint_id format: {endpoint_id!r}"


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_MODELS_YAML = REPO_ROOT / "config" / "models.yaml"


def test_production_models_yaml_schema_contract():
    """The real catalog stays parseable and unambiguous."""
    import yaml

    data = yaml.safe_load(PRODUCTION_MODELS_YAML.read_text())
    models = data["models"]
    assert models, "production catalog must declare at least one model"

    ids = [m["id"] for m in models]
    aliases = [alias for m in models for alias in (m.get("aliases") or [])]
    assert len(ids) == len(set(ids)), "duplicate model ids in models.yaml"
    assert len(aliases) == len(set(aliases)), "duplicate aliases in models.yaml"
    assert not set(ids) & set(aliases), "alias shadows a canonical model id"
    for model in models:
        assert model.get("name"), f"model {model['id']!r} missing name"
        assert model.get("provider"), f"model {model['id']!r} missing provider"


def test_production_models_yaml_registers_full_inventory(monkeypatch):
    """With every env-backed credential present, the whole catalog registers.

    Injects a dummy value for each ``${VAR}`` referenced by models.yaml so no
    model is skipped for missing credentials, then asserts the registered
    route set is exactly canonical ids plus aliases. This is the regression
    net for the future overlay migration: the same file loaded from a new
    location must produce this same inventory. (Counts are derived from the
    file, so routine catalog edits do not churn this test.)
    """
    import re

    import yaml

    raw = PRODUCTION_MODELS_YAML.read_text()
    for var in sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", raw))):
        monkeypatch.setenv(var, "http://contract-dummy.test")

    data = yaml.safe_load(raw)
    chat_expected: set[str] = set()
    embedding_expected: set[str] = set()
    for model in data["models"]:
        target = embedding_expected if model.get("type") == "embedding" else chat_expected
        target.add(model["id"])
        target.update(model.get("aliases") or [])

    # Invoke exactly like bootstrap does: embedding models bypass the
    # RouteExecutor and land in embedding_adapters instead of chat routes.
    exe = RouteExecutor()
    embedding_adapters: dict = {}
    count, infos = registry.register_from_models_yaml(
        exe,
        PRODUCTION_MODELS_YAML,
        embedding_adapters=embedding_adapters,
        continue_on_missing_env=True,
    )
    assert set(exe.routes) == chat_expected
    assert set(embedding_adapters) == embedding_expected
    # The registration count covers chat routes and embedding entries alike.
    assert count == len(chat_expected) + len(embedding_expected)
    # Registration infos cover every canonical model, embeddings included
    # (the /v1/models listing needs them even though they bypass the router).
    assert {info.model_id for info in infos} == {m["id"] for m in data["models"]}
    for name, route in exe.routes.items():
        assert route.adapters, f"route {name!r} registered without adapters"
        for adapter, _weight in route.adapters:
            assert adapter.config.endpoint_id
