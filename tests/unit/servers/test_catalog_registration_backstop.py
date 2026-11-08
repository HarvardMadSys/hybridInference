"""A stored catalog serves all of its models or none, and never stops startup.

Startup reads the catalog from the database, where Admin keeps it, and Admin
runs in the same process: a catalog every worker refused to start on would
lock the administrator out with the gateway. One that cannot be registered
serves nothing instead, and says why, naming the model. Never a part of it:
registration that stopped at a bad entry served the models before it without
their RouteWise settings and dropped the models after it.
"""

from __future__ import annotations

import logging

import pytest

from routing.routers import FixedRouter
from serving.servers import bootstrap


def _model(model_id: str, **route_fields) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "provider": "openai_compat",
        "router": "fixed",
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


def _serve(monkeypatch, document) -> None:
    monkeypatch.setattr(bootstrap, "get_document", lambda kind: document)


def _errors(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


# --- startup never stops on a stored catalog, and never serves part of one -----


@pytest.mark.parametrize(
    "middle",
    [
        _model("invalid-middle", provider="Not A Slug!"),
        _model("invalid-middle", include_usage_in_stream="false"),
        _model("invalid-middle", embeddings_path="/v1/../embeddings"),
        {**_model("invalid-middle"), "router": "nimbus"},
    ],
    ids=["provider label", "capability flag", "embeddings path", "router"],
)
async def test_startup_serves_none_of_a_catalog_with_a_refused_entry(monkeypatch, caplog, middle):
    _serve(monkeypatch, _catalog(middle))
    router = FixedRouter()

    embedding, infos = await bootstrap._init_router_and_models(router)

    # Every model served has its registration info -- here, none of them.
    assert (embedding, infos, router.routes) == ({}, [], {})
    assert any("invalid-middle" in message for message in _errors(caplog))


async def test_startup_survives_a_catalog_that_is_not_json(monkeypatch, caplog):
    for name in ("MODEL_CATALOG",):
        monkeypatch.setenv(name, "models:\n  - id: yaml-not-json\n")
    router = FixedRouter()

    assert await bootstrap._init_router_and_models(router) == ({}, [])

    assert router.routes == {}
    assert any("must be a valid JSON object" in message for message in _errors(caplog))


async def test_startup_does_not_serve_an_inline_credential_admin_refuses(monkeypatch, caplog):
    secret = "sk-live-inline-0099"
    model = _model("inline", api_key=secret)
    _serve(monkeypatch, {"models": [model]})
    router = FixedRouter()

    assert await bootstrap._init_router_and_models(router) == ({}, [])

    assert router.routes == {}
    assert "must reference a separate secret setting" in caplog.text
    assert secret not in caplog.text


async def test_a_skipped_model_keeps_the_others_and_their_routewise_settings(monkeypatch):
    monkeypatch.delenv("UNSET_BACKSTOP_KEY", raising=False)
    routewise = {**_model("routewise-before"), "router": "routewise"}
    skipped = _model("skipped-middle", api_key="${UNSET_BACKSTOP_KEY}")
    _serve(monkeypatch, {"models": [routewise, skipped, _model("valid-after")]})
    router = FixedRouter()

    _embedding, infos = await bootstrap._init_router_and_models(router)

    assert set(router.routes) == {"routewise-before", "valid-after"}
    assert {info.model_id: info.router for info in infos} == {
        "routewise-before": "routewise",
        "valid-after": "fixed",
    }
