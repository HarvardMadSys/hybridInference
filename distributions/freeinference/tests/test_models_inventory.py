"""This deployment's model catalogue, asserted where the catalogue lives.

Both cases read the registry itself and check its shape and its full
inventory — claims about what FreeInference serves, not about what the
gateway can do. They moved here with models.yaml; upstream keeps the
schema contracts that apply to any registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest  # noqa: F401
import yaml

from routing.executor import RouteExecutor
from serving.servers import registry

MODELS_YAML = Path(__file__).resolve().parents[1] / "config" / "models.yaml"


def test_production_models_yaml_schema_contract():
    """The real catalog stays parseable without alias/canonical collisions."""

    data = yaml.safe_load(MODELS_YAML.read_text())
    models = data["models"]
    assert models, "production catalog must declare at least one model"

    ids = [m["id"] for m in models]
    aliases = [alias for m in models for alias in (m.get("aliases") or [])]
    assert len(ids) == len(set(ids)), "duplicate model ids in models.yaml"
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

    raw = MODELS_YAML.read_text()
    for var in sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", raw))):
        monkeypatch.setenv(var, "http://contract-dummy.test")

    data = yaml.safe_load(raw)
    chat_expected: set[str] = set()
    embedding_expected: set[str] = set()
    chat_alias_owner: dict[str, str] = {}
    registration_count_expected = 0
    for model in data["models"]:
        aliases = model.get("aliases") or []
        target = embedding_expected if model.get("type") == "embedding" else chat_expected
        target.add(model["id"])
        target.update(aliases)
        registration_count_expected += 1 + len(aliases)
        if model.get("type") != "embedding":
            # Duplicate aliases are accepted today; later declarations replace
            # earlier RouteConfig entries. Pin that deterministic behavior.
            for alias in aliases:
                chat_alias_owner[alias] = model["id"]

    # Invoke exactly like bootstrap does: embedding models bypass the
    # RouteExecutor and land in embedding_adapters instead of chat routes.
    exe = RouteExecutor()
    embedding_adapters: dict = {}
    count, infos = registry.register_from_models_yaml(
        exe,
        MODELS_YAML,
        embedding_adapters=embedding_adapters,
        continue_on_missing_env=True,
    )
    assert set(exe.routes) == chat_expected
    assert set(embedding_adapters) == embedding_expected
    # Count records declarations processed, including duplicate aliases, while
    # the route dictionaries above naturally contain unique final keys.
    assert count == registration_count_expected
    # Registration infos cover every canonical model, embeddings included
    # (the /v1/models listing needs them even though they bypass the router).
    assert {info.model_id for info in infos} == {m["id"] for m in data["models"]}
    for name, route in exe.routes.items():
        assert route.adapters, f"route {name!r} registered without adapters"
        for adapter, _weight in route.adapters:
            assert adapter.config.endpoint_id
    for alias, canonical_id in chat_alias_owner.items():
        assert exe.routes[alias] is exe.routes[canonical_id]
