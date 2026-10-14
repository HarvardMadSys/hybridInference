"""An alias is held to its model's access rules on every inference surface.

A registry alias shares its model's route, but the state that decides who may
call the model is keyed by the canonical id: the admin's runtime visibility
override (the admin API refuses to set one on an alias), the caller's own
denylist, and an inference grant's model list. A check that reads the spelling
the caller used finds none of it. The override and the denylist stop applying,
and a grant that names the model is refused it.

Chat Completions has always read the canonical id off the route. The Anthropic
surface canonicalized only Anthropic's display names, so a registry alias went
past all three there, ``count_tokens`` included. Both now apply the one check in
``serving/model_access.py``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import anthropic_messages, completions

from .test_completions import DummyAdapter, VisibilityResolver, _mk_cfg

MODEL = "canonical-model"
ALIAS = "model-alias"


def _ctx(**extra) -> dict:
    return {"user_id": "user-1", "role": "free", "authenticated": True, "is_admin": False, **extra}


def _app(module, user_ctx: dict, mock_db_logger, overrides: dict[str, str] | None = None):
    router_exec = RouteExecutor()
    router_exec.register_route(MODEL, [(DummyAdapter(_mk_cfg(MODEL)), 1.0)], aliases=[ALIAS])

    app = FastAPI()
    app.state.services = AppServices(
        router=router_exec,
        db_logger=mock_db_logger,
        model_visibility_resolver=VisibilityResolver(overrides or {}),
    )
    install_error_handlers(app)
    app.dependency_overrides[verify_api_key] = lambda: user_ctx
    app.include_router(module.router)
    return app


async def _call(app: FastAPI, path: str, body: dict) -> int:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return (await client.post(path, json=body)).status_code


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
        anthropic_messages,
        "/anthropic/v1/messages/count_tokens",
        lambda model: {"model": model, "messages": [{"role": "user", "content": "Hi"}]},
        id="count-tokens",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_an_alias_is_held_to_its_models_visibility_override(
    module, path, body, mock_db_logger
):
    """The admin raised the model to admin-only; the alias must not reopen it."""
    app = _app(module, _ctx(), mock_db_logger, overrides={MODEL: "admin"})

    assert await _call(app, path, body(ALIAS)) == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_an_alias_is_held_to_the_callers_denylist(module, path, body, mock_db_logger):
    """A user who disabled the model has disabled every name it answers to."""
    app = _app(module, _ctx(disabled_models=[MODEL]), mock_db_logger)

    assert await _call(app, path, body(ALIAS)) == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_grant_that_names_the_model_reaches_it_by_alias(module, path, body, mock_db_logger):
    """A grant stores canonical ids; the model it names is the model it reaches."""
    app = _app(module, _ctx(agent_allowed_models=[MODEL]), mock_db_logger)

    assert await _call(app, path, body(ALIAS)) == status.HTTP_200_OK


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_an_alias_of_an_open_model_still_resolves(module, path, body, mock_db_logger):
    """The other half: nothing above may be passed by refusing every alias."""
    app = _app(module, _ctx(), mock_db_logger)

    assert await _call(app, path, body(ALIAS)) == status.HTTP_200_OK
