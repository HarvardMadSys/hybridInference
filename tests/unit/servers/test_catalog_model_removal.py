"""Removing a model from the stored catalog, and other processes following that."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from routing.routers import FixedRouter
from serving.config import app_config
from serving.config.documents import encode, without_model
from serving.servers.registry import register_from_models_document
from serving.servers.routers.admin.provider_routes import (
    BASELINE_ENTRIES_ATTR,
    retire_models_removed_from_catalog,
)
from tests.fixtures.app_config_store import FakeAppConfigStore, row


def _model(model_id: str, *, alias: str | None = None) -> dict:
    entry = {
        "id": model_id,
        "provider": "vllm",
        "base_url": "http://localhost:9/v1",
    }
    if alias is not None:
        entry["aliases"] = [alias]
    return entry


def _register(*models: dict) -> FixedRouter:
    router = FixedRouter()
    register_from_models_document(router, {"models": list(models)})
    return router


def _namespace(router: FixedRouter) -> SimpleNamespace:
    return SimpleNamespace(
        router=router,
        model_router_registry=None,
        model_router_transition_locks={},
        managed_routers=[],
    )


async def _load(text: str) -> FakeAppConfigStore:
    store = FakeAppConfigStore([row("MODEL_CATALOG", text)])
    await app_config.load(store, bootstrap=False)
    app_config.attach(store)
    return store


def test_without_model_keeps_every_other_model_and_every_other_key():
    document = {
        "auto": {"enabled": False},
        "x-local": {"kind": "vllm"},
        "models": [_model("gone", alias="gone-alias"), _model("keep-me"), "not-an-entry"],
    }

    updated, removed = without_model(document, "gone")

    assert removed is True
    assert updated["models"] == [_model("keep-me"), "not-an-entry"]
    assert updated["auto"] == {"enabled": False}
    assert updated["x-local"] == {"kind": "vllm"}
    assert document["models"][0]["id"] == "gone"


def test_without_model_leaves_a_catalog_that_does_not_list_the_model():
    document = {"models": [_model("keep-me")]}

    updated, removed = without_model(document, "gone")

    assert removed is False
    assert updated is document


@pytest.mark.unit
async def test_retirement_drops_a_config_model_the_catalog_no_longer_lists():
    router = _register(
        _model("gone", alias="gone-alias"),
        _model("keep-me"),
        _model("runtime-one"),
    )
    setattr(router.routes["runtime-one"], BASELINE_ENTRIES_ATTR, [])
    await _load(encode({"models": [_model("keep-me")], "auto": {"enabled": False}}))

    await retire_models_removed_from_catalog(_namespace(router))

    assert "gone" not in router.routes
    assert "gone-alias" not in router.routes
    assert "keep-me" in router.routes
    assert "runtime-one" in router.routes


@pytest.mark.unit
async def test_an_empty_catalog_retires_config_models_and_keeps_runtime_models():
    router = _register(_model("gone", alias="gone-alias"), _model("runtime-one"))
    setattr(router.routes["runtime-one"], BASELINE_ENTRIES_ATTR, [])
    await _load(encode({"models": None}))

    await retire_models_removed_from_catalog(_namespace(router))

    assert "gone" not in router.routes
    assert "gone-alias" not in router.routes
    assert "runtime-one" in router.routes


@pytest.mark.unit
async def test_retirement_does_nothing_until_configuration_is_loaded():
    router = _register(_model("keep-me"))

    await retire_models_removed_from_catalog(_namespace(router))

    assert "keep-me" in router.routes


@pytest.mark.unit
async def test_an_invalid_catalog_does_not_retire_config_models():
    router = _register(_model("keep-me", alias="keep-alias"))
    await _load("not-json")
    assert app_config.validation_problem("MODEL_CATALOG")

    await retire_models_removed_from_catalog(_namespace(router))

    assert "keep-me" in router.routes
    assert "keep-alias" in router.routes
