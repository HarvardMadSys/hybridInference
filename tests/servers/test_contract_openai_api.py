"""Contract freeze: OpenAI-compatible API surface.

End-to-end characterization of the /v1/chat/completions envelope, the SSE
stream framing, the error envelope, and the /v1/models item shape — through
the real routers with a stub adapter (no network). These pin today's public
API behavior so the distribution split
(docs/agents/specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)
can refactor config/branding with a regression net underneath.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import compat, completions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL_ID = "contract-model"


class _StubAdapter(BaseAdapter):
    """Mimics an OpenAI-compat upstream, including the ``data: [DONE]`` frame."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        usage = UsageInfo(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        return self.format_response(content="contract-ok", model=self.config.id, usage=usage)

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params):
        yield self.format_stream_chunk(model=self.config.id, content="contract-")
        yield self.format_stream_chunk(model=self.config.id, content="ok")
        yield "data: [DONE]\n\n"


@pytest.fixture
async def contract_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    router = RouteExecutor()
    cfg = ModelConfig(id=MODEL_ID, name=MODEL_ID, provider="test", base_url="http://test")
    router.register_route(MODEL_ID, [(_StubAdapter(cfg), 1.0)])

    app = FastAPI()
    install_error_handlers(app)
    app.state.services = AppServices(router=router, db_logger=None)  # type: ignore[attr-defined]
    app.include_router(compat.router)
    app.include_router(completions.router)

    async def _anon_user():
        return {
            "user_id": "anonymous",
            "user_name": None,
            "authenticated": False,
            "quota_remaining_cost_usd": float("inf"),
        }

    app.dependency_overrides[verify_api_key] = _anon_user
    return app


@pytest.fixture
async def contract_client(contract_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=contract_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_chat_completion_envelope(contract_client: AsyncClient):
    resp = await contract_client.post(
        "/v1/chat/completions",
        json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert isinstance(body["created"], int)
    assert body["model"] == MODEL_ID

    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "contract-ok"
    assert choice["finish_reason"] == "stop"

    usage = body["usage"]
    assert usage["prompt_tokens"] == 3
    assert usage["completion_tokens"] == 2
    assert usage["total_tokens"] == 5


@pytest.mark.asyncio
async def test_streaming_sse_contract(contract_client: AsyncClient):
    resp = await contract_client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    # SSE events are separated by a blank line; each event of this stream is
    # exactly one single-line data frame. Splitting on "\n\n" (not "\n")
    # freezes the event-boundary framing itself.
    events = [event for event in resp.text.split("\n\n") if event.strip()]
    assert events, "stream produced no events"
    for event in events:
        assert event.startswith("data: ")
        assert "\n" not in event, f"multi-line event frame: {event!r}"
    # An upstream [DONE] frame passes through to the client verbatim.
    assert events[-1] == "data: [DONE]"

    import json

    chunks = [json.loads(event[len("data: ") :]) for event in events[:-1]]
    assert chunks, "no JSON chunks before [DONE]"
    for chunk in chunks:
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["id"].startswith("chatcmpl-")
        assert chunk["model"] == MODEL_ID
        assert "delta" in chunk["choices"][0]

    # The gateway prepends a role preamble chunk before upstream content.
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    content = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert content == "contract-ok"


@pytest.mark.asyncio
async def test_unknown_model_error_envelope(contract_client: AsyncClient):
    resp = await contract_client.post(
        "/v1/chat/completions",
        json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert error["code"] == 404
    assert error["type"] == "unknown"
    assert "no-such-model" in error["message"]


@pytest.mark.asyncio
async def test_v1_models_openai_item_shape(test_client):
    resp = await test_client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"], "expected at least one model in the listing"
    item = body["data"][0]
    assert item["object"] == "model"
    assert isinstance(item["id"], str) and item["id"]
    assert isinstance(item["created"], int)
    assert "owned_by" in item
