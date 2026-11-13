"""A credential-scoped model answers only to its scope's keys, on every surface.

The scope rides the user context as ``credential_scope`` (set by the key auth
paths only when the request is attributed to the key) and is read by the one
access check every surface applies. For everyone else the model is answered
exactly as an unknown model is, admins included, and a scoped key likewise
cannot see or call an ordinary model — so neither side can tell what the
other side has.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.model_access import CREDENTIAL_SCOPE_KEY
from serving.servers.auth import optional_verify_api_key, verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import anthropic_messages, completions, models, responses

from .test_completions import DummyAdapter, _mk_cfg

SCOPED = "scoped-model"
ORDINARY = "ordinary-model"


def _ctx(role: str = "pro", scope: str | None = None) -> dict:
    ctx = {
        "user_id": "owner-1",
        "role": role,
        "authenticated": True,
        "is_admin": role == "admin",
        "api_key_id": 21,
    }
    if scope is not None:
        ctx[CREDENTIAL_SCOPE_KEY] = scope
    return ctx


def _app(router_cls, user_ctx: dict, mock_db_logger) -> FastAPI:
    router_exec = FixedRouter()
    router_exec.register_route(
        SCOPED,
        [(DummyAdapter(_mk_cfg(SCOPED)), 1.0)],
        required_role="pro",
        credential_scope="agent",
    )
    router_exec.register_route(ORDINARY, [(DummyAdapter(_mk_cfg(ORDINARY)), 1.0)])

    app = FastAPI()
    app.state.services = AppServices(router=router_exec, db_logger=mock_db_logger)
    install_error_handlers(app)
    app.dependency_overrides[verify_api_key] = lambda: user_ctx
    app.dependency_overrides[optional_verify_api_key] = lambda: user_ctx
    app.include_router(router_cls.router)
    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


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
    pytest.param(
        responses,
        "/v1/responses",
        lambda model: {"model": model, "input": "Hi"},
        id="responses",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
@pytest.mark.parametrize("role", ["free", "pro", "admin"])
async def test_an_ordinary_credential_cannot_call_a_scoped_model(
    module, path, body, role, mock_db_logger
):
    app = _app(module, _ctx(role=role), mock_db_logger)
    async with await _client(app) as client:
        refused = await client.post(path, json=body(SCOPED))
        unknown = await client.post(path, json=body("no-such-model"))

    assert refused.status_code == status.HTTP_404_NOT_FOUND
    assert refused.status_code == unknown.status_code
    assert refused.json() == json.loads(json.dumps(unknown.json()).replace("no-such-model", SCOPED))


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_scoped_key_calls_the_scoped_model_and_nothing_else(
    module, path, body, mock_db_logger
):
    app = _app(module, _ctx(role="free", scope="agent"), mock_db_logger)
    async with await _client(app) as client:
        allowed = await client.post(path, json=body(SCOPED))
        refused = await client.post(path, json=body(ORDINARY))

    assert allowed.status_code != status.HTTP_404_NOT_FOUND
    assert refused.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_listings_are_scoped_both_ways(mock_db_logger):
    scoped_app = _app(models, _ctx(role="free", scope="agent"), mock_db_logger)
    async with await _client(scoped_app) as client:
        listed = (await client.get("/v1/models")).json()
    assert [model["id"] for model in listed["data"]] == [SCOPED]

    pro_app = _app(models, _ctx(role="pro"), mock_db_logger)
    async with await _client(pro_app) as client:
        listed = (await client.get("/v1/models")).json()
    assert [model["id"] for model in listed["data"]] == [ORDINARY]

    admin_app = _app(models, _ctx(role="admin"), mock_db_logger)
    async with await _client(admin_app) as client:
        listed = (await client.get("/v1/models")).json()
    assert [model["id"] for model in listed["data"]] == [ORDINARY]
