"""A non-streaming chat to a model a registered engine queue serves goes upstream as a stream.

The engine's queue gives a request's place back at the engine's first output, so
a non-streaming request would hold one for its whole generation. On the online
chat routes it is sent upstream as a stream and buffered into the same
non-streaming answer, whatever ``force_chat_completions_streaming`` says, unless
only the non-streaming path can serve it (a JSON ``response_format``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    get_upstream_limiter,
    reset_upstream_limiter,
)
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions
from serving.servers.routers.completions import ChatOptions
from serving.stream import done_sentinel, make_final_usage_chunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MODEL = "local-model"
ENGINE_URL = "http://localhost:18003/v1"
MESSAGES = [{"role": "user", "content": "Hi"}]
PROMPT_TOKENS, COMPLETION_TOKENS = 7, 2


class _Engine(BaseAdapter):
    """A self-hosted engine that gives the same reply whichever way it is asked."""

    def __init__(self) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name=MODEL,
                provider="engine",
                base_url=ENGINE_URL,
                context_length=8192,
                max_output_length=4096,
                supported_params=["temperature", "max_tokens"],
            )
        )
        self.reply = "Test response"
        self.calls: list[str] = []

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self.calls.append("chat")
        return self.format_response(
            content=self.reply,
            model=self.config.id,
            usage=UsageInfo(
                prompt_tokens=PROMPT_TOKENS,
                completion_tokens=COMPLETION_TOKENS,
                total_tokens=PROMPT_TOKENS + COMPLETION_TOKENS,
            ),
        )

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        self.calls.append("stream")
        yield self.format_stream_chunk(model=self.config.id, content=self.reply[:5])
        yield self.format_stream_chunk(model=self.config.id, content=self.reply[5:])
        yield make_final_usage_chunk(
            model=self.config.id,
            messages=messages,
            total_content=self.reply,
            prompt_tokens_override=PROMPT_TOKENS,
            completion_tokens_override=COMPLETION_TOKENS,
        )
        yield done_sentinel()


@pytest.fixture(autouse=True)
def _limiter(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    reset_upstream_limiter(UpstreamConcurrencyLimiter(initial_limit=4, max_limit=4))
    yield
    reset_upstream_limiter()


async def _post(engine: _Engine, **body: Any) -> Any:
    """Send one non-streaming chat through the real route, the global setting off."""
    runtime_settings = MagicMock()
    runtime_settings.get_bool = AsyncMock(return_value=False)
    router = FixedRouter()
    router.register_route(MODEL, [(engine, 1.0)])
    app = FastAPI()
    app.state.services = AppServices(
        router=router, db_logger=None, log_store=None, runtime_settings=runtime_settings
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": MESSAGES, "stream": False, **body},
        )


def _register() -> None:
    get_upstream_limiter().set_engine_queues(MODEL, {ENGINE_URL: 1})


async def test_a_non_streaming_request_to_a_registered_engine_is_buffered():
    _register()
    engine = _Engine()

    resp = await _post(engine)

    assert engine.calls == ["stream"]
    assert resp.status_code == status.HTTP_200_OK
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert (body["object"], body["choices"][0]["message"]["content"]) == (
        "chat.completion",
        "Test response",
    )


async def test_the_buffered_answer_is_the_non_streaming_one():
    plain_engine = _Engine()
    plain = (await _post(plain_engine)).json()
    _register()
    buffered_engine = _Engine()
    buffered = (await _post(buffered_engine)).json()

    assert (plain_engine.calls, buffered_engine.calls) == (["chat"], ["stream"])

    def comparable(body: dict[str, Any]) -> dict[str, Any]:
        # Generated per response either way.
        return {key: value for key, value in body.items() if key not in {"id", "created"}}

    assert json.dumps(comparable(buffered), sort_keys=True) == json.dumps(
        comparable(plain), sort_keys=True
    )


async def test_a_request_asking_for_json_keeps_the_non_streaming_path():
    _register()
    engine = _Engine()
    engine.reply = '{"ok": true}'

    resp = await _post(engine, response_format={"type": "json_object"})

    # Only the non-streaming path checks a reply against the format and asks again.
    assert resp.status_code == status.HTTP_200_OK
    assert engine.calls == ["chat"]


async def test_a_model_no_registered_queue_serves_stays_non_streaming():
    engine = _Engine()

    resp = await _post(engine)

    assert resp.status_code == status.HTTP_200_OK
    assert engine.calls == ["chat"]


def test_only_the_online_routes_buffer_for_an_engine_queue():
    assert ChatOptions.online(None).buffers_for_engine_queues
    assert not ChatOptions.delegated().buffers_for_engine_queues
    assert not ChatOptions.batch().buffers_for_engine_queues
