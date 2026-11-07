"""An API key with a model allowlist reaches those models and no others (P2a).

The allowlist rides the user context as ``key_allowed_models`` (set by the key
auth paths only when the request is attributed to the key) and is read by the
one access check every surface applies. A model outside it is answered exactly
as an unknown model is, on inference and in the listings alike, so a leaked
limited key cannot tell which other models the account could reach.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.model_access import (
    KEY_ALLOWED_MODELS_KEY,
    ModelAccessDenial,
    is_model_outside_key_scope,
    model_access_denial,
)
from serving.servers.auth import optional_verify_api_key, verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import anthropic_messages, completions, models

from .test_completions import DummyAdapter, _mk_cfg

ALLOWED = "allowed-model"
OTHER = "other-model"


def _key_ctx(allowed: list[str] | None) -> dict:
    ctx = {
        "user_id": "owner-1",
        "role": "pro",
        "authenticated": True,
        "is_admin": False,
        "api_key_id": 21,
    }
    if allowed is not None:
        ctx[KEY_ALLOWED_MODELS_KEY] = allowed
    return ctx


def _app(router_cls, user_ctx: dict, mock_db_logger) -> FastAPI:
    router_exec = FixedRouter()
    for model in (ALLOWED, OTHER):
        router_exec.register_route(model, [(DummyAdapter(_mk_cfg(model)), 1.0)])

    app = FastAPI()
    app.state.services = AppServices(router=router_exec, db_logger=mock_db_logger)
    install_error_handlers(app)
    app.dependency_overrides[verify_api_key] = lambda: user_ctx
    app.dependency_overrides[optional_verify_api_key] = lambda: user_ctx
    app.include_router(router_cls.router)
    return app


async def _post(app: FastAPI, path: str, body: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body)


_SURFACES = (
    pytest.param(
        completions,
        "/v1/chat/completions",
        lambda model: {"model": model, "messages": [{"role": "user", "content": "Hi"}]},
        id="chat-completions",
    ),
    pytest.param(
        anthropic_messages,
        "/anthropic/v1/messages",
        lambda model: {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Hi"}],
        },
        id="anthropic-messages",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_key_cannot_call_a_model_outside_its_allowlist(module, path, body, mock_db_logger):
    app = _app(module, _key_ctx([ALLOWED]), mock_db_logger)

    refused = await _post(app, path, body(OTHER))
    unknown = await _post(app, path, body("no-such-model"))

    assert refused.status_code == status.HTTP_404_NOT_FOUND
    # Indistinguishable from a model that does not exist, apart from the name
    # it quotes back.
    assert refused.status_code == unknown.status_code
    assert refused.json() == json.loads(json.dumps(unknown.json()).replace("no-such-model", OTHER))


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_key_can_call_the_models_it_allows(module, path, body, mock_db_logger):
    app = _app(module, _key_ctx([ALLOWED]), mock_db_logger)

    assert (await _post(app, path, body(ALLOWED))).status_code != status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_key_without_an_allowlist_is_unaffected(module, path, body, mock_db_logger):
    app = _app(module, _key_ctx(None), mock_db_logger)

    for model in (ALLOWED, OTHER):
        assert (await _post(app, path, body(model))).status_code != status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_the_model_listing_shows_only_the_keys_models(mock_db_logger):
    app = _app(models, _key_ctx([ALLOWED]), mock_db_logger)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = (await client.get("/v1/models")).json()

    assert [model["id"] for model in listed["data"]] == [ALLOWED]


def _route():
    adapter = SimpleNamespace(config=SimpleNamespace(id="canonical-model"))
    return SimpleNamespace(
        published=True, required_role="free", admin_only=False, adapters=[(adapter, 1.0)]
    )


@pytest.mark.asyncio
async def test_the_allowlist_applies_to_listings_unlike_a_grant():
    ctx = _key_ctx(["somewhere-else"])

    for grant_scope in (True, False):
        assert (
            await model_access_denial(_route(), "canonical-model", ctx, grant_scope=grant_scope)
            is ModelAccessDenial.OUTSIDE_KEY
        )


@pytest.mark.asyncio
async def test_an_alias_is_held_to_its_models_allowlist_entry():
    ctx = _key_ctx(["canonical-model"])

    assert await model_access_denial(_route(), "an-alias", ctx) is None


def test_absent_means_unrestricted():
    assert is_model_outside_key_scope(ALLOWED, {"user_id": "u"}) is False
    assert is_model_outside_key_scope(ALLOWED, None) is False
    assert is_model_outside_key_scope(OTHER, {KEY_ALLOWED_MODELS_KEY: [ALLOWED]}) is True
