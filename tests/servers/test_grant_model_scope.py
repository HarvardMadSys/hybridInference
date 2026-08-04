"""A grant may call the models it names, and no others.

**This is the only scope an inference grant carries.** It names one user, one
attempt, a short lifetime and a model list; everything except that list governs
*whether* the credential is live rather than *what* it can reach. So an
unenforced list is not a narrower grant — it is no scope at all, and a leaked
token reaches everything the owner's role does.

That was the state until this file existed. `authenticate_grant_model_call`
wrote `agent_allowed_models` into the user context and **nothing read it**: one
writer, zero readers, in a field whose whole job is to deny. The bug was
invisible from either end — the mint clamped correctly and the tests for it
passed, the inference path enforced role and denylist and its tests passed, and
no test crossed the two.

Worse, the containment it was meant to replace had already been removed. The
per-attempt worker token this succeeded bounded a leak by *budget* — its own
source said so: "the blast radius of a leaked token is bounded by the budget,
not by the model tier". The budget went when the per-task budget went. Between
those two changes a grant carried no effective limit but its five-minute TTL.

Both inference surfaces are covered because both resolve models themselves.
Chat Completions and Anthropic Messages are separate handlers with separate
gates, and a fix applied to one is exactly the shape of bug that ships.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.model_access import is_model_outside_grant_scope
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import anthropic_messages, completions

from .test_completions import DummyAdapter, _mk_cfg

GRANTED = "granted-model"
UNGRANTED = "ungranted-model"


def _grant_ctx(allowed: list[str] | None, **extra) -> dict:
    """The context `authenticate_grant_model_call` hands the inference path."""
    ctx = {
        "user_id": "owner-1",
        "role": "pro",
        "authenticated": True,
        "is_admin": False,
        "agent_job_id": "ajob_1",
        "agent_grant_id": "agr_1",
        **extra,
    }
    if allowed is not None:
        ctx["agent_allowed_models"] = allowed
    return ctx


def _app(router_cls, user_ctx: dict, mock_db_logger) -> FastAPI:
    router_exec = RouteExecutor()
    for model in (GRANTED, UNGRANTED):
        router_exec.register_route(model, [(DummyAdapter(_mk_cfg(model)), 1.0)])

    app = FastAPI()
    app.state.services = AppServices(router=router_exec, db_logger=mock_db_logger)
    install_error_handlers(app)
    app.dependency_overrides[verify_api_key] = lambda: user_ctx
    app.include_router(router_cls.router)
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
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_grant_cannot_call_a_model_it_does_not_name(module, path, body, mock_db_logger):
    """**The case this file exists for.**

    The model is published, the owner's role can reach it, and the owner has
    not disabled it. The only thing standing between the caller and it is the
    grant's own list.
    """
    app = _app(module, _grant_ctx([GRANTED]), mock_db_logger)

    assert await _call(app, path, body(UNGRANTED)) == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_grant_can_call_the_model_it_names(module, path, body, mock_db_logger):
    """The other half. A gate that refuses everything would pass the test above
    while breaking every agent job, and nothing else here would notice."""
    app = _app(module, _grant_ctx([GRANTED]), mock_db_logger)

    assert await _call(app, path, body(GRANTED)) != status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_grant_is_still_subject_to_the_owners_denylist(module, path, body, mock_db_logger):
    """A grant is the owner calling through a sandbox, so a model they disabled
    for themselves stays disabled — even though the mint listed it.

    The grant path did not carry `disabled_models` at all, so a per-user
    control silently stopped applying the moment the call arrived from an
    agent.
    """
    ctx = _grant_ctx([GRANTED], disabled_models=[GRANTED])
    app = _app(module, ctx, mock_db_logger)

    assert await _call(app, path, body(GRANTED)) == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_an_ordinary_api_key_is_unaffected(module, path, body, mock_db_logger):
    """No `agent_allowed_models` key means no grant, and no new restriction.

    Absence and emptiness mean opposite things here; getting that backwards
    would deny every non-agent request on the platform.
    """
    ctx = {"user_id": "user-1", "role": "pro", "authenticated": True, "is_admin": False}
    app = _app(module, ctx, mock_db_logger)

    assert await _call(app, path, body(UNGRANTED)) != status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_grant_that_resolved_to_nothing_calls_nothing(module, path, body, mock_db_logger):
    """An empty list is a grant whose requested models were all outside the
    role — the mint already decided the answer was nothing.

    `None` at mint means "everything this role can reach", so an empty list can
    only be an empty intersection. Reading it as "unrestricted" would invert
    exactly the case the mint had already refused.
    """
    app = _app(module, _grant_ctx([]), mock_db_logger)

    assert await _call(app, path, body(GRANTED)) == status.HTTP_404_NOT_FOUND


def test_the_policy_distinguishes_absent_from_empty() -> None:
    """Stated once, directly, because both call sites depend on it."""
    assert is_model_outside_grant_scope(GRANTED, {"agent_allowed_models": [GRANTED]}) is False
    assert is_model_outside_grant_scope(UNGRANTED, {"agent_allowed_models": [GRANTED]}) is True
    assert is_model_outside_grant_scope(GRANTED, {"agent_allowed_models": []}) is True
    # Not a grant at all.
    assert is_model_outside_grant_scope(GRANTED, {"user_id": "u"}) is False
    assert is_model_outside_grant_scope(GRANTED, None) is False
