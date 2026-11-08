"""Registration of a model catalog is all or nothing.

Registration that stopped at a bad entry left everything before it registered:
on the router, in the embedding table, and in the provider-key registry, which
then handed runtime keys to adapters nothing served. Every entry is checked and
every adapter built before any of that is written now.
"""

from __future__ import annotations

import pytest

from routing.routers import FixedRouter
from serving.adapters import dynamic_keys
from serving.servers import registry
from serving.servers.registry import RegistryConfigError, register_from_models_document


def _model(model_id: str, **route_fields) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "provider": "openai_compat",
        "route": [
            {
                "kind": "openai_compat",
                "weight": 1.0,
                "base_url": f"https://{model_id}.example/v1",
                **route_fields,
            }
        ],
    }


def _catalog(middle: dict) -> dict:
    return {"models": [_model("valid-before"), middle, _model("valid-after")]}


def test_a_refused_entry_registers_nothing_at_all():
    router, adapters = FixedRouter(), {}
    middle = _model("invalid-middle", provider="Not A Slug!")

    with pytest.raises(RegistryConfigError, match="invalid-middle"):
        register_from_models_document(router, _catalog(middle), adapters)

    assert router.routes == {}
    assert adapters == {}
    assert dynamic_keys.get_known_providers() == set()


def test_an_adapter_that_will_not_build_registers_nothing_and_names_its_model(monkeypatch):
    build = registry._make_adapter

    def make_adapter(kind, cfg):
        if cfg["id"] == "broken-middle":
            raise RuntimeError("factory failed")
        return build(kind, cfg)

    monkeypatch.setattr(registry, "_make_adapter", make_adapter)
    router = FixedRouter()

    with pytest.raises(RegistryConfigError, match="model 'broken-middle' could not be registered"):
        register_from_models_document(router, _catalog(_model("broken-middle")), {})

    assert router.routes == {}
    assert dynamic_keys.get_known_providers() == set()


def test_a_catalog_that_registers_still_registers_every_model():
    router = FixedRouter()

    count, infos = register_from_models_document(router, _catalog(_model("middle")), {})

    assert count == 3
    assert set(router.routes) == {info.model_id for info in infos}
    assert set(router.routes) == {"valid-before", "middle", "valid-after"}
    assert dynamic_keys.get_known_providers() == {"openai_compat"}
