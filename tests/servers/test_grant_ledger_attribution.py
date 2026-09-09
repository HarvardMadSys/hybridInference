"""A grant-authenticated call leaves the keys the usage window reads back.

The window report (``GET /internal/agent-grants/{id}/usage?since=…``) selects
rows by the grant recorded in ``api_logs.metadata`` and places them by the
request's real start time. Neither existed on the row before this: the
surfaces wrote only ``agent_job_id``, and the row's ``timestamp`` is the
insert time of a write scheduled after the response completed. Every surface
a grant can reach is covered because each builds its own metadata, and the
Responses surface Codex uses delegates to chat completions.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.grant_auth import LEDGER_ATTRIBUTION_VERSION
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import anthropic_messages, completions

from .test_completions import DummyAdapter, _mk_cfg, _wait_for_db_log_kwargs
from .test_embeddings_logging import (
    _build_app as _build_embeddings_app,
    _CapturingLogger,
    _FakeAdapter,
    _ok_response,
    _post as _post_embeddings,
)

MODEL = "granted-model"
GRANT_CTX = {
    "user_id": "owner-1",
    "role": "pro",
    "authenticated": True,
    "is_admin": False,
    "agent_job_id": "thread:athr_1",
    "agent_grant_id": "agr_1",
    "agent_allowed_models": [MODEL],
}
PLAIN_CTX = {"user_id": "owner-1", "role": "pro", "authenticated": True, "is_admin": False}
ATTRIBUTION_KEYS = {"agent_grant_id", "request_started_at", "attribution_version"}


def _app(module, user_ctx: dict, mock_db_logger, mock_log_store) -> FastAPI:
    router_exec = RouteExecutor()
    router_exec.register_route(MODEL, [(DummyAdapter(_mk_cfg(MODEL)), 1.0)])
    app = FastAPI()
    app.state.services = AppServices(
        router=router_exec, db_logger=mock_db_logger, log_store=mock_log_store
    )
    install_error_handlers(app)
    app.dependency_overrides[verify_api_key] = lambda: user_ctx
    app.include_router(module.router)
    return app


async def _post(app: FastAPI, path: str, body: dict) -> int:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return (await client.post(path, json=body)).status_code


def _assert_attributed(metadata: dict, *, before: float) -> None:
    assert metadata["agent_job_id"] == "thread:athr_1"
    assert metadata["agent_grant_id"] == "agr_1"
    assert metadata["attribution_version"] == LEDGER_ATTRIBUTION_VERSION
    started = datetime.fromisoformat(metadata["request_started_at"])
    assert started.tzinfo is not None
    assert datetime.fromtimestamp(before, tz=UTC) <= started <= datetime.now(UTC)


_SURFACES = (
    pytest.param(
        completions,
        "/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        id="chat-completions",
    ),
    pytest.param(
        anthropic_messages,
        "/anthropic/v1/messages",
        {"model": MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": "Hi"}]},
        id="anthropic-messages",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_a_grant_call_is_attributed_to_its_grant_and_start_time(
    module, path, body, mock_db_logger, mock_log_store
):
    app = _app(module, GRANT_CTX, mock_db_logger, mock_log_store)
    before = time.time()
    assert await _post(app, path, body) == status.HTTP_200_OK
    kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert kwargs is not None, "the call must be logged"
    _assert_attributed(kwargs["metadata"], before=before)


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "path", "body"), _SURFACES)
async def test_an_ordinary_call_carries_no_grant_attribution(
    module, path, body, mock_db_logger, mock_log_store
):
    app = _app(module, PLAIN_CTX, mock_db_logger, mock_log_store)
    assert await _post(app, path, body) == status.HTTP_200_OK
    kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert kwargs is not None
    assert not ATTRIBUTION_KEYS & set(kwargs["metadata"])


@pytest.mark.asyncio
async def test_an_embedding_call_is_attributed_too():
    """Embeddings build their metadata separately from the chat surfaces."""
    logger = _CapturingLogger()
    app = _build_embeddings_app(_FakeAdapter(response=_ok_response()), logger)
    app.dependency_overrides[verify_api_key] = lambda: {**GRANT_CTX, "role": "internal"}
    before = time.time()
    resp = await _post_embeddings(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == status.HTTP_200_OK
    assert logger.calls, "the call must be logged"
    _assert_attributed(logger.calls[0][1]["metadata"], before=before)
