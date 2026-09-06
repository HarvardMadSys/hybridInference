"""Session labelling on /v1/chat/completions.

The chat surface used to read only ``X-Session-ID``. These tests pin what a
coding agent's own declaration now does: it labels the log row *and* reaches
the router, which is where session-scoped prefix-cache accounting reads it.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "gpt-4"


class ParamCapturingAdapter(BaseAdapter):
    """Adapter that records the params the router handed it."""

    def __init__(self, config: ModelConfig) -> None:
        super().__init__(config)
        self.seen_params: dict[str, Any] = {}

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        self.seen_params = params
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self.seen_params = params
        yield "data: [DONE]\n\n"


def _cfg() -> ModelConfig:
    return ModelConfig(
        id=MODEL,
        name=MODEL,
        provider="test",
        base_url="http://test",
        context_length=8192,
        max_output_length=4096,
        supported_params=["temperature", "max_tokens"],
    )


async def _wait_for_log_kwargs(mock_log_store, timeout: float = 2.0) -> dict[str, Any] | None:
    """Wait for the fire-and-forget log task and return its kwargs."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock_log_store.log_request.call_count > 0:
            return mock_log_store.log_request.call_args.kwargs
        await asyncio.sleep(0.05)
    return None


@pytest.fixture
def session_app(
    monkeypatch, mock_db_logger, mock_log_store
) -> tuple[FastAPI, ParamCapturingAdapter]:
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    adapter = ParamCapturingAdapter(_cfg())
    router = RouteExecutor()
    router.register_route(MODEL, [(adapter, 1.0)])

    app = FastAPI(title="Session Identity Test App")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
        log_store=mock_log_store,
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    return app, adapter


async def _post(app: FastAPI, headers: dict[str, str], body: dict[str, Any] | None = None):
    payload = {"model": MODEL, "messages": [{"role": "user", "content": "Hi"}]}
    payload.update(body or {})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/v1/chat/completions", json=payload, headers=headers)


@pytest.mark.asyncio
async def test_agent_session_header_labels_the_row_and_reaches_routing(session_app, mock_log_store):
    """Codex CLI stamps ``session-id`` on its requests; both consumers see it."""
    app, adapter = session_app
    resp = await _post(app, {"session-id": "codex-run-1"})

    assert resp.status_code == status.HTTP_200_OK
    kwargs = await _wait_for_log_kwargs(mock_log_store)
    assert kwargs is not None, "log_request was never called"
    assert kwargs["metadata"]["session_id"] == "codex-run-1"
    assert kwargs["metadata"]["session_id_source"] == "session-id"
    # The router (and so RouteWise's session-scoped prefix-cache accounting)
    # sees the same value the log row carries.
    assert adapter.seen_params["session_id"] == "codex-run-1"


@pytest.mark.asyncio
async def test_canonical_header_still_wins(session_app, mock_log_store):
    app, _adapter = session_app
    resp = await _post(
        app,
        {"X-Session-ID": "canonical", "session-id": "codex-run-1"},
        {"metadata": {"session_id": "from-body"}},
    )

    assert resp.status_code == status.HTTP_200_OK
    kwargs = await _wait_for_log_kwargs(mock_log_store)
    assert kwargs is not None
    assert kwargs["metadata"]["session_id"] == "canonical"
    assert kwargs["metadata"]["session_id_source"] == "x-session-id"


@pytest.mark.asyncio
async def test_body_metadata_session_id_recorded(session_app, mock_log_store):
    app, adapter = session_app
    resp = await _post(app, {}, {"metadata": {"session_id": "body-run"}})

    assert resp.status_code == status.HTTP_200_OK
    kwargs = await _wait_for_log_kwargs(mock_log_store)
    assert kwargs is not None
    assert kwargs["metadata"]["session_id"] == "body-run"
    assert kwargs["metadata"]["session_id_source"] == "metadata.session_id"
    assert adapter.seen_params["session_id"] == "body-run"


@pytest.mark.asyncio
async def test_no_declaration_leaves_the_row_unlabelled(session_app, mock_log_store):
    """Without a declaration nothing is invented — and nothing reaches routing."""
    app, adapter = session_app
    resp = await _post(app, {})

    assert resp.status_code == status.HTTP_200_OK
    kwargs = await _wait_for_log_kwargs(mock_log_store)
    assert kwargs is not None
    assert "session_id" not in kwargs["metadata"]
    assert "session_id_source" not in kwargs["metadata"]
    assert "session_id" not in adapter.seen_params


@pytest.mark.asyncio
async def test_unusable_declaration_is_dropped(session_app, mock_log_store):
    """An over-long declaration is rejected, not truncated into the column."""
    app, adapter = session_app
    resp = await _post(app, {"session-id": "s" * 200})

    assert resp.status_code == status.HTTP_200_OK
    kwargs = await _wait_for_log_kwargs(mock_log_store)
    assert kwargs is not None
    assert "session_id" not in kwargs["metadata"]
    assert "session_id" not in adapter.seen_params
