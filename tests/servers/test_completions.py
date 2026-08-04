"""Integration tests for /v1/chat/completions aligned with current server.

Covers non-streaming and streaming flows, model-not-found, invalid payload,
and fallback behavior using injected services.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from routing.model_router_registry import ModelRouterRegistry
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.middleware.request_log import RequestLogMiddleware
from serving.servers.routers import compat, completions, health, models
from serving.stream import done_sentinel, make_final_usage_chunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


from serving.servers.auth import verify_api_key


@pytest.fixture(autouse=True)
def disable_auth_for_completions_tests(monkeypatch):
    """Disable auth for routing-focused completions tests."""
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("serving.servers.auth.is_user_auth_enabled", lambda: False)
    yield
    get_settings.cache_clear()


class DummyAdapter(BaseAdapter):
    stream_calls = 0

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        content = params.get("content", "Test response")
        resp = self.format_response(content=content, model=self.config.id)
        return resp

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        type(self).stream_calls += 1
        # Emit role and content chunks then final usage
        yield self.format_stream_chunk(model=self.config.id, content="Test ")
        yield self.format_stream_chunk(model=self.config.id, content="response")
        yield make_final_usage_chunk(
            model=self.config.id, messages=messages, total_content="Test response"
        )
        yield done_sentinel()


def test_token_usage_sanity_allows_reasoning_counted_inside_completion():
    assert completions._token_usage_is_sane(
        prompt_tokens=100,
        completion_tokens=50,
        reasoning_tokens=20,
        total_tokens=150,
    )
    assert completions._token_usage_is_sane(
        prompt_tokens=100,
        completion_tokens=50,
        reasoning_tokens=20,
        total_tokens=170,
    )
    assert not completions._token_usage_is_sane(
        prompt_tokens=100,
        completion_tokens=50,
        reasoning_tokens=20,
        total_tokens=149,
    )


class ThinkingDeltaAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="answer", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        thinking_chunk = {
            "id": "chatcmpl-thinking",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "delta": {"thinking": "Thinking in alternate field..."},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(thinking_chunk)}\n\n"
        yield self.format_stream_chunk(model=self.config.id, content="answer")
        yield make_final_usage_chunk(
            model=self.config.id,
            messages=messages,
            total_content="Thinking in alternate field...answer",
        )
        yield done_sentinel()


class SplitToolCallNameAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("not implemented")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield (
            'data: {"id":"tool-1","object":"chat.completion.chunk",'
            '"created":123,"model":"gpt-4",'
            '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            '"id":"call_1","type":"function","function":{"name":"get_",'
            '"arguments":"{\\"city"}}]},"finish_reason":null}]}\n\n'
        )
        yield (
            'data: {"id":"tool-1","object":"chat.completion.chunk",'
            '"created":123,"model":"gpt-4",'
            '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            '"function":{"name":"weather","arguments":"\\":\\"Boston\\"}"}}]},'
            '"finish_reason":"tool_calls"}]}\n\n'
        )
        yield make_final_usage_chunk(model=self.config.id, messages=messages, total_content="")
        yield done_sentinel()


class FailingAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("Primary adapter failed")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        raise RuntimeError("Primary adapter failed")
        if False:  # pragma: no cover
            yield ""


class AdapterWithReasoningContent(BaseAdapter):
    """Adapter that emits reasoning_content field like Zhipu GLM-4.6."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        content = params.get("content", "Test response")
        resp = self.format_response(
            content=content,
            model=self.config.id,
            reasoning_content="Let me think through this carefully.",
        )
        return resp

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        # Emit a chunk with reasoning_content (non-standard field)
        chunk_with_reasoning = (
            'data: {"id": "test-123", "object": "chat.completion.chunk", '
            '"created": 1234567890, "model": "' + self.config.id + '", '
            '"choices": [{"index": 0, "delta": {"role": "assistant", "content": "", '
            '"reasoning_content": "\\n"}, "finish_reason": null}]}\n\n'
        )
        yield chunk_with_reasoning

        # Emit normal content chunks
        yield self.format_stream_chunk(model=self.config.id, content="Test ")
        yield self.format_stream_chunk(model=self.config.id, content="response")
        yield make_final_usage_chunk(
            model=self.config.id, messages=messages, total_content="Test response"
        )
        yield done_sentinel()


class NonStreamReasoningOnlyAdapter(BaseAdapter):
    """Adapter that returns only reasoning_content in non-stream mode."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(
            content="",
            model=self.config.id,
            reasoning_content="Internal reasoning only.",
        )

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield done_sentinel()


class RoutingAwareAdapter(BaseAdapter):
    """Adapter that returns explicit internal routing metadata."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        response = self.format_response(content="Synthetic response", model=self.config.id)
        response["_routing"] = {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
        }
        return response

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="Synthetic ")
        yield self.format_stream_chunk(model=self.config.id, content="response")
        yield done_sentinel()


class VisibilityResolver:
    def __init__(self, overrides: dict[str, str]):
        self._overrides = overrides

    async def get_effective_required_role(self, model_id: str, default_role: str) -> str:
        return self._overrides.get(model_id, default_role)


def _mk_cfg(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="test",
        base_url="http://test",
        context_length=8192,
        max_output_length=4096,
        supported_params=["temperature", "top_p", "max_tokens"],
    )


@pytest.fixture
async def completions_app(monkeypatch, mock_db_logger, mock_log_store) -> FastAPI:
    """Create a FastAPI app with completions/compat routers and injected services.

    Note: We set app.state.services directly to avoid relying on lifespan handling
    in the test transport.
    """

    # Disable auth for routing-focused tests to avoid auth noise.
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route("gpt-4", [(DummyAdapter(_mk_cfg("gpt-4")), 1.0)])

    app = FastAPI(title="Test Completions App")
    # Inject services on state directly (no lifespan dependency in tests)
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
        log_store=mock_log_store,
    )

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(completions.router)
    app.include_router(compat.router)
    return app


@pytest.fixture
async def completions_client(completions_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=completions_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_non_streaming_basic(
    completions_client: AsyncClient,
    completions_app: FastAPI,
):
    active_router = completions_app.state.services.router
    active_router.record_observation = MagicMock()

    resp = await completions_client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["model"] == "gpt-4"
    assert body["choices"][0]["message"]["content"] == "Test response"
    observation = active_router.record_observation.call_args.args[0]
    assert observation.request_id.startswith("req_")


@pytest.mark.asyncio
async def test_upstream_401_reaches_the_request_log_attributed_and_at_info(
    monkeypatch, mock_db_logger, mock_log_store, caplog
):
    """A relayed upstream 401 must not be filed as a routine auth challenge.

    Regression for a production outage: a local proxy answered 401 to 100% of
    requests for an hour and the only artifact was a DEBUG line, because
    ``RequestLogMiddleware`` sees a status code and could not tell the gateway's
    own auth challenge from an upstream refusing the gateway's credential. Two
    halves fix it — the handler republishes the upstream attribution into
    ``req_ctx`` once the ``req_ctx.push`` scope around the adapter call has
    unwound, and the middleware demotes only *unattributed* 401s — and the second
    is a no-op without the first. So this drives the real route instead of
    restating either half on a hand-written one.
    """
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    class _Unauthorized(Exception):
        """An upstream credential rejection, as aiohttp surfaces it."""

        status = 401

    class RejectingAdapter(BaseAdapter):
        async def chat_completion(self, messages: list[dict[str, Any]], **params):
            raise _Unauthorized("HTTP 401: invalid api key")

        async def stream_chat_completion(
            self, messages: list[dict[str, Any]], **params
        ) -> AsyncGenerator[str, None]:
            raise _Unauthorized("HTTP 401: invalid api key")
            if False:  # pragma: no cover
                yield ""

    cfg = ModelConfig(
        id="diffusiongemma",
        name="diffusiongemma",
        provider="diffusiongemma-local",
        base_url="http://localhost:8002/v1",
        context_length=8192,
        max_output_length=4096,
        supported_params=["temperature", "max_tokens"],
    )
    router = RouteExecutor()
    router.register_route("diffusiongemma", [(RejectingAdapter(cfg), 1.0)])

    app = FastAPI(title="Upstream 401 App")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
        log_store=mock_log_store,
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    app.add_middleware(RequestLogMiddleware)

    transport = ASGITransport(app=app)
    with caplog.at_level(logging.INFO, logger="serving.servers.middleware.request_log"):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "diffusiongemma",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    records = [r for r in caplog.records if r.getMessage() == "http_request"]
    assert len(records) == 1, "expected exactly one request log line"
    record = records[0]
    assert record.status_code == 401
    # The handler published the failing upstream, so the middleware can tell this
    # from a gateway-issued challenge and keeps it at INFO instead of DEBUG.
    assert record.provider == "diffusiongemma-local"
    assert record.levelno == logging.INFO


@pytest.mark.asyncio
async def test_streaming_sse_format(completions_client: AsyncClient):
    async with completions_client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
    ) as resp:
        assert resp.status_code == status.HTTP_200_OK
        lines: list[str] = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                lines.append(line)
        assert any(line == "data: [DONE]" for line in lines)
        # Concatenate content chunks (ignore usage chunk)
        content = "".join(
            json.loads(line[6:])["choices"][0]["delta"].get("content", "")
            for line in lines
            if line != "data: [DONE]" and line != "data: {}"
        )
        assert content == "Test response"


def _content_from_sse_lines(lines: list[str]) -> str:
    content_parts: list[str] = []
    for line in lines:
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[6:])
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content_parts.append(delta.get("content") or "")
    return "".join(content_parts)


@pytest.mark.asyncio
async def test_runtime_setting_streams_upstream_and_buffers_non_stream_response(
    completions_app: FastAPI,
):
    runtime_settings = MagicMock()
    runtime_settings.get_bool = AsyncMock(return_value=True)
    completions_app.state.services.runtime_settings = runtime_settings
    DummyAdapter.stream_calls = 0

    transport = ASGITransport(app=completions_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

    runtime_settings.get_bool.assert_awaited_once_with("force_chat_completions_streaming")
    assert DummyAdapter.stream_calls == 1
    assert resp.status_code == status.HTTP_200_OK
    assert resp.headers["content-type"].startswith("application/json")
    assert "x-hybridinference-streaming-response" not in resp.headers
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Test response"


@pytest.mark.asyncio
async def test_forced_streaming_buffers_thinking_delta_into_reasoning_content(monkeypatch):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    runtime_settings = MagicMock()
    runtime_settings.get_bool = AsyncMock(return_value=True)

    router = RouteExecutor()
    router.register_route(
        "thinking-model", [(ThinkingDeltaAdapter(_mk_cfg("thinking-model")), 1.0)]
    )

    app = FastAPI(title="Forced Streaming Thinking App")
    app.state.services = AppServices(
        router=router,
        db_logger=None,
        log_store=None,
        runtime_settings=runtime_settings,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "thinking-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    message = body["choices"][0]["message"]
    assert message["content"] == "answer"
    assert message["reasoning_content"] == "Thinking in alternate field..."


@pytest.mark.asyncio
async def test_runtime_forced_buffered_probe_preserves_provider_header():
    runtime_settings = MagicMock()
    runtime_settings.get_bool = AsyncMock(return_value=True)
    router = RouteExecutor()
    cfg = _mk_cfg("gpt-4")
    cfg.provider = "single-provider"
    router.register_route("gpt-4", [(DummyAdapter(cfg), 1.0)])

    app = FastAPI(title="Forced Buffered Probe Header App")
    app.state.services = AppServices(
        router=router,
        db_logger=None,
        log_store=None,
        runtime_settings=runtime_settings,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"X-Probe": "synthetic"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.headers["X-Provider"] == "single-provider"


@pytest.mark.asyncio
async def test_runtime_forced_buffered_stream_merges_split_tool_call_names():
    runtime_settings = MagicMock()
    runtime_settings.get_bool = AsyncMock(return_value=True)
    router = RouteExecutor()
    router.register_route("gpt-4", [(SplitToolCallNameAdapter(_mk_cfg("gpt-4")), 1.0)])

    app = FastAPI(title="Forced Buffered Tool Call App")
    app.state.services = AppServices(
        router=router,
        db_logger=None,
        log_store=None,
        runtime_settings=runtime_settings,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert resp.status_code == status.HTTP_200_OK
    tool_call = resp.json()["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "get_weather"
    assert tool_call["function"]["arguments"] == '{"city":"Boston"}'


@pytest.mark.asyncio
async def test_runtime_forced_buffered_stream_returns_error_when_all_upstreams_fail(
    monkeypatch,
    mock_log_store,
):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    runtime_settings = MagicMock()
    runtime_settings.get_bool = AsyncMock(return_value=True)
    router = RouteExecutor()
    router.register_route(
        "fail-model",
        [(UpstreamStatusErrorAdapter(_mk_cfg("fail-model"), status_code=503), 1.0)],
    )

    app = FastAPI(title="Forced Buffered Failure App")
    app.state.services = AppServices(
        router=router,
        db_logger=None,
        log_store=mock_log_store,
        runtime_settings=runtime_settings,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "fail-model", "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    db_kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert db_kwargs is not None, "DB log_request should have been called on error path"
    assert db_kwargs["status_code"] == 503


@pytest.mark.asyncio
async def test_runtime_forced_buffered_stream_uses_router_fallback_before_content():
    runtime_settings = MagicMock()
    runtime_settings.get_bool = AsyncMock(return_value=True)
    router = RouteExecutor()
    primary_cfg = _mk_cfg("gpt-4")
    primary_cfg.provider = "primary"
    backup_cfg = _mk_cfg("gpt-4")
    backup_cfg.provider = "backup"
    router.register_route(
        "gpt-4", [(FailingAdapter(primary_cfg), 0.9), (DummyAdapter(backup_cfg), 0.1)]
    )

    app = FastAPI(title="Forced Buffered Fallback App")
    app.state.services = AppServices(
        router=router,
        db_logger=None,
        log_store=None,
        runtime_settings=runtime_settings,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    random_state = random.random
    transport = ASGITransport(app=app)
    try:
        random.random = lambda: 0.01
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            )
    finally:
        random.random = random_state

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["choices"][0]["message"]["content"] == "Test response"


@pytest.mark.asyncio
async def test_runtime_forced_buffered_stream_preserves_all_circuits_open_503(
    monkeypatch,
    mock_log_store,
):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    runtime_settings = MagicMock()
    runtime_settings.get_bool = AsyncMock(return_value=True)
    router = RouteExecutor()
    adapter = DummyAdapter(_mk_cfg("circuit-open-model"))
    router.register_route("circuit-open-model", [(adapter, 1.0)])
    monkeypatch.setattr(router._health_registry, "allow_request", lambda _endpoint_id: False)

    app = FastAPI(title="Forced Buffered Circuit Open App")
    app.state.services = AppServices(
        router=router,
        db_logger=None,
        log_store=mock_log_store,
        runtime_settings=runtime_settings,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "circuit-open-model",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    db_kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert db_kwargs is not None, "DB log_request should have been called on error path"
    assert db_kwargs["status_code"] == 503


@pytest.mark.asyncio
async def test_model_not_found_returns_404(completions_client: AsyncClient):
    resp = await completions_client.post(
        "/v1/chat/completions",
        json={"model": "unknown", "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    data = resp.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_unpublished_model_returns_404_before_routewise_router_lookup(
    completions_app: FastAPI,
):
    router = completions_app.state.services.router
    router.register_route(
        "staged-model",
        [(DummyAdapter(_mk_cfg("staged-model")), 1.0)],
        published=False,
    )
    model_router_registry = ModelRouterRegistry(
        models_config={},
        default_router_name="routewise",
        shared_fixed_router=router,
    )
    assert model_router_registry.get_router_name("staged-model") == "routewise"
    get_router = MagicMock(wraps=model_router_registry.get_router)
    model_router_registry.get_router = get_router
    completions_app.state.services.model_router_registry = model_router_registry

    transport = ASGITransport(app=completions_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "staged-model",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    get_router.assert_not_called()


@pytest.mark.asyncio
async def test_model_hidden_by_runtime_visibility_returns_404(completions_app: FastAPI):
    completions_app.state.services.model_visibility_resolver = VisibilityResolver(
        {"gpt-4": "admin"}
    )
    completions_app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "free-user",
        "role": "free",
        "authenticated": True,
        "is_admin": False,
    }

    transport = ASGITransport(app=completions_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_invalid_request_returns_400(completions_client: AsyncClient):
    # Missing required fields
    resp = await completions_client.post("/v1/chat/completions", json={"model": "gpt-4"})
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_fallback_on_primary_failure(completions_app: FastAPI):
    # Rebuild router with failing primary and working fallback
    router = RouteExecutor()
    router.register_route(
        "gpt-4", [(FailingAdapter(_mk_cfg("gpt-4")), 0.9), (DummyAdapter(_mk_cfg("gpt-4")), 0.1)]
    )

    services = AppServices(router=router, db_logger=None, log_store=None)

    app = FastAPI(title="Fallback App")
    app.state.services = services  # type: ignore[attr-defined]
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        # When fallback occurs, router strips _routing before returning to user in non-streaming
        # path; our server keeps _routing only internally for db logging. We validate content.
        assert data["choices"][0]["message"]["content"] == "Test response"


@pytest.mark.asyncio
async def test_fallback_success_logs_failed_primary_attempt_as_diagnostic(
    monkeypatch,
    mock_log_store,
):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    router = RouteExecutor()
    primary_cfg = _mk_cfg("gpt-4")
    primary_cfg.provider = "primary"
    primary_cfg.endpoint_id = "primary:endpoint"
    backup_cfg = _mk_cfg("gpt-4")
    backup_cfg.provider = "backup"
    router.register_route(
        "gpt-4", [(FailingAdapter(primary_cfg), 0.9), (DummyAdapter(backup_cfg), 0.1)]
    )

    app = FastAPI(title="Fallback Logging App")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=None,
        log_store=mock_log_store,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    random_state = random.random
    transport = ASGITransport(app=app)
    try:
        random.random = lambda: 0.01
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            )
    finally:
        random.random = random_state

    assert resp.status_code == status.HTTP_200_OK
    kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert kwargs is not None, "log_request was never called"
    assert kwargs["status_code"] == 200
    assert "error" not in kwargs
    assert kwargs["metadata"]["upstream_error"] == (
        "Upstream fallback after primary:endpoint: RuntimeError: Primary adapter failed"
    )


@pytest.mark.asyncio
async def test_non_stream_failure_logged_even_when_observation_raises(
    monkeypatch,
    mock_log_store,
):
    """A throwing routing-observation update must not drop the error log.

    On the failure path ``record_routing_observation`` runs before the DB log is
    scheduled. An online-learning router's ``record_observation`` does real work
    and can raise; if it did, the failed request was previously dropped from
    ``api_logs`` entirely. It must still be persisted.
    """
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    cfg = _mk_cfg("gpt-4")
    cfg.provider = "primary"
    router = RouteExecutor()
    router.register_route("gpt-4", [(FailingAdapter(cfg), 1.0)])
    # Mimic an online-learning router whose observation update raises.
    router.record_observation = MagicMock(side_effect=RuntimeError("observation boom"))

    app = FastAPI(title="Observation Throw App")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=None,
        log_store=mock_log_store,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
        )

    # The failure still surfaces as a 5xx to the client...
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    # ...and the failed request is still persisted despite the observation throw.
    db_kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert db_kwargs is not None, "DB log_request should have been called on error path"
    assert db_kwargs["status_code"] == 500
    assert db_kwargs["error"]


@pytest.mark.asyncio
async def test_synthetic_probe_skips_db_logging(monkeypatch, mock_db_logger):
    """Synthetic probe traffic should not be logged to DB."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route("gpt-4", [(RoutingAwareAdapter(_mk_cfg("gpt-4")), 1.0)])

    app = FastAPI(title="Synthetic Probe Test")
    mock_log_store = MagicMock()
    mock_log_store.log_request = AsyncMock()
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
        log_store=mock_log_store,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"X-Probe": "synthetic"},
        )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.headers.get("X-Provider") == "test"
    mock_log_store.log_request.assert_not_called()


@pytest.mark.asyncio
async def test_reasoning_content_filtered_in_streaming(monkeypatch, mock_db_logger):
    """X-Reasoning-Passthrough: false strips reasoning_content from streaming output."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route("glm-4.6", [(AdapterWithReasoningContent(_mk_cfg("glm-4.6")), 1.0)])

    app = FastAPI(title="Test Strict Mode")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "glm-4.6",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers={"X-Reasoning-Passthrough": "false"},
        ) as resp,
    ):
        assert resp.status_code == status.HTTP_200_OK
        lines: list[str] = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                lines.append(line)

        # Strict mode: reasoning_content should NOT appear
        for line in lines:
            if line != "data: [DONE]" and line != "data: {}":
                try:
                    chunk = json.loads(line[6:])
                    if chunk.get("choices"):
                        delta = chunk["choices"][0].get("delta", {})
                        assert "reasoning_content" not in delta, (
                            "reasoning_content should be stripped in strict mode"
                        )
                except json.JSONDecodeError:
                    pass

        # Content should still arrive intact
        content = "".join(
            json.loads(line[6:])["choices"][0]["delta"].get("content", "")
            for line in lines
            if line != "data: [DONE]" and line != "data: {}"
        )
        assert content == "Test response"


@pytest.mark.asyncio
async def test_reasoning_content_visible_by_default_in_streaming(monkeypatch, mock_db_logger):
    """Default /v1/chat/completions behavior preserves reasoning_content."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route("glm-4.6", [(AdapterWithReasoningContent(_mk_cfg("glm-4.6")), 1.0)])

    app = FastAPI(title="Test Default Passthrough")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "glm-4.6",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        assert resp.status_code == status.HTTP_200_OK
        lines: list[str] = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                lines.append(line)

        saw_reasoning = False
        for line in lines:
            if line != "data: [DONE]" and line != "data: {}":
                try:
                    chunk = json.loads(line[6:])
                    if chunk.get("choices"):
                        delta = chunk["choices"][0].get("delta", {})
                        if "reasoning_content" in delta:
                            saw_reasoning = True
                except json.JSONDecodeError:
                    pass
        assert saw_reasoning, "reasoning_content should be visible by default"

        content = "".join(
            json.loads(line[6:])["choices"][0]["delta"].get("content", "")
            for line in lines
            if line != "data: [DONE]" and line != "data: {}"
        )
        assert content == "Test response"


@pytest.mark.asyncio
async def test_streaming_reasoning_content_persisted_to_db(
    monkeypatch, mock_db_logger, mock_log_store
):
    """Streaming reasoning_content is accumulated into the DB-logged response."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route("glm-4.6", [(AdapterWithReasoningContent(_mk_cfg("glm-4.6")), 1.0)])

    app = FastAPI(title="Test Streaming Reasoning Persisted")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
        log_store=mock_log_store,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "glm-4.6",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        assert resp.status_code == status.HTTP_200_OK
        async for _ in resp.aiter_lines():
            pass

    kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert kwargs is not None, "log_request was never called"
    response = kwargs["response"]
    assert isinstance(response, dict)
    message = response["choices"][0]["message"]
    assert "reasoning_content" in message
    assert message["reasoning_content"]


@pytest.mark.asyncio
async def test_non_stream_strict_strips_reasoning_content(monkeypatch, mock_db_logger):
    """X-Reasoning-Passthrough: false strips reasoning_content from non-streaming response."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route("glm-4.6", [(AdapterWithReasoningContent(_mk_cfg("glm-4.6")), 1.0)])

    app = FastAPI(title="Test Non-Stream Strict Mode")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "glm-4.6", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"X-Reasoning-Passthrough": "false"},
        )

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    message = body["choices"][0]["message"]
    assert message["content"] == "Test response"
    assert "reasoning_content" not in message


@pytest.mark.asyncio
async def test_non_stream_default_preserves_reasoning_content(monkeypatch, mock_db_logger):
    """Default non-streaming response preserves reasoning_content."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route("glm-4.6", [(AdapterWithReasoningContent(_mk_cfg("glm-4.6")), 1.0)])

    app = FastAPI(title="Test Non-Stream Default Passthrough")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "glm-4.6", "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    message = body["choices"][0]["message"]
    assert message["content"] == "Test response"
    assert message["reasoning_content"] == "Let me think through this carefully."


@pytest.mark.asyncio
async def test_non_stream_reasoning_only_strict_returns_empty_visible_output(
    monkeypatch, mock_db_logger
):
    """Strict mode (X-Reasoning-Passthrough: false) should hide reasoning-only output."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route("glm-5", [(NonStreamReasoningOnlyAdapter(_mk_cfg("glm-5")), 1.0)])

    app = FastAPI(title="Test Non-Stream Reasoning Only Strict")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "glm-5", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"X-Reasoning-Passthrough": "false"},
        )

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    message = body["choices"][0]["message"]
    assert message["content"] == ""
    assert "reasoning_content" not in message


@pytest.mark.asyncio
async def test_non_stream_reasoning_only_passthrough_preserves_reasoning(
    monkeypatch, mock_db_logger
):
    """Passthrough non-streaming mode should expose reasoning-only responses."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route("glm-5", [(NonStreamReasoningOnlyAdapter(_mk_cfg("glm-5")), 1.0)])

    app = FastAPI(title="Test Non-Stream Reasoning Only Passthrough")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
    )
    install_error_handlers(app)
    app.include_router(completions.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "glm-5", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"X-Reasoning-Passthrough": "true"},
        )

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    message = body["choices"][0]["message"]
    assert message["content"] == ""
    assert message["reasoning_content"] == "Internal reasoning only."


# ---------------------------------------------------------------------------
# Admin-only enforcement tests for /v1/chat/completions
# ---------------------------------------------------------------------------


def _build_admin_gate_app(user_ctx: dict, mock_db_logger) -> FastAPI:
    """Build test app with an admin_only model and injected user_ctx."""
    router_exec = RouteExecutor()
    router_exec.register_route("public-model", [(DummyAdapter(_mk_cfg("public-model")), 1.0)])
    router_exec.register_route(
        "secret-model", [(DummyAdapter(_mk_cfg("secret-model")), 1.0)], admin_only=True
    )

    app = FastAPI()
    app.state.services = AppServices(
        router=router_exec,
        db_logger=mock_db_logger,
    )
    install_error_handlers(app)
    app.dependency_overrides[verify_api_key] = lambda: user_ctx
    app.include_router(completions.router)
    return app


@pytest.mark.asyncio
async def test_admin_only_rejected_for_non_admin(mock_db_logger):
    """Non-admin user calling admin_only model gets 404."""
    app = _build_admin_gate_app(
        {"user_id": "user1", "authenticated": True, "is_admin": False},
        mock_db_logger,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "secret-model", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_admin_only_allowed_for_admin(mock_db_logger):
    """Admin user calling admin_only model gets 200."""
    app = _build_admin_gate_app(
        {"user_id": "admin1", "authenticated": True, "is_admin": True, "role": "admin"},
        mock_db_logger,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "secret-model", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["choices"][0]["message"]["content"] == "Test response"


@pytest.mark.asyncio
async def test_disabled_model_returns_404(mock_db_logger):
    """Per-user disabled model denylist behaves as not found."""
    app = _build_admin_gate_app(
        {
            "user_id": "user1",
            "authenticated": True,
            "is_admin": False,
            "role": "free",
            "disabled_models": ["public-model"],
        },
        mock_db_logger,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "public-model", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# TTFT (Time To First Token) tests
# ---------------------------------------------------------------------------


class ReasoningOnlyAdapter(BaseAdapter):
    """Adapter that emits only reasoning_content before content (deepseek-r1 scenario)."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="answer", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        # First chunk: only reasoning_content, no content
        reasoning_chunk = {
            "id": "chatcmpl-r1",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": "Let me think step by step..."},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(reasoning_chunk)}\n\n"

        # Then content
        yield self.format_stream_chunk(model=self.config.id, content="answer")
        yield make_final_usage_chunk(
            model=self.config.id, messages=messages, total_content="answer"
        )
        yield done_sentinel()


class OpenRouterReasoningOnlyAdapter(BaseAdapter):
    """Adapter that emits OpenRouter-style delta.reasoning before content."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="answer", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        reasoning_chunk = {
            "id": "chatcmpl-or-reasoning",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning": "Thinking in OpenRouter field..."},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(reasoning_chunk)}\n\n"
        yield self.format_stream_chunk(model=self.config.id, content="answer")
        yield make_final_usage_chunk(
            model=self.config.id, messages=messages, total_content="answer"
        )
        yield done_sentinel()


class SlowStartAdapter(BaseAdapter):
    """Adapter that stalls before the first visible chunk."""

    def __init__(self, config: ModelConfig, delay_s: float = 0.05) -> None:
        super().__init__(config)
        self.delay_s = delay_s

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="late", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        await asyncio.sleep(self.delay_s)
        yield self.format_stream_chunk(model=self.config.id, content="late")
        yield make_final_usage_chunk(model=self.config.id, messages=messages, total_content="late")
        yield done_sentinel()


class ToolCallsOnlyAdapter(BaseAdapter):
    """Adapter that emits reasoning followed by tool calls and no text."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        reasoning_chunk = {
            "id": "chatcmpl-tool-reasoning",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": "Thinking about which file to inspect..."},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(reasoning_chunk)}\n\n"
        yield self.format_tool_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_read_file_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"app/main.py"}',
                    },
                }
            ],
            model=self.config.id,
        )
        yield make_final_usage_chunk(
            model=self.config.id,
            messages=messages,
            total_content="",
            finish_reason="tool_calls",
        )
        yield done_sentinel()


class TrueEmptyTerminalAdapter(BaseAdapter):
    """Adapter that ends after reasoning without content or tool calls."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        reasoning_chunk = {
            "id": "chatcmpl-empty-terminal",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": "I am thinking, but never answering."},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(reasoning_chunk)}\n\n"
        yield make_final_usage_chunk(model=self.config.id, messages=messages, total_content="")
        yield done_sentinel()


class ErrorAfterFirstTokenAdapter(BaseAdapter):
    """Adapter that emits one content chunk then raises an error."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("not implemented")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        yield self.format_stream_chunk(model=self.config.id, content="partial")
        raise RuntimeError("upstream connection lost")


class ErrorBeforeAnyTokenAdapter(BaseAdapter):
    """Adapter that raises immediately without yielding any content."""

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("not implemented")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        raise RuntimeError("upstream refused connection")
        if False:  # pragma: no cover
            yield ""


class UpstreamStatusErrorAdapter(BaseAdapter):
    """Adapter that raises an upstream HTTP status during streaming."""

    def __init__(
        self, config: ModelConfig, status_code: int, error_body: str | None = None
    ) -> None:
        super().__init__(config)
        self.status_code = status_code
        self.error_body = error_body

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        raise RuntimeError("not implemented")

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        exc = aiohttp.ClientResponseError(
            request_info=MagicMock(real_url="http://upstream.test/v1/chat/completions"),
            history=(),
            status=self.status_code,
            message="Service Unavailable",
            headers=None,
        )
        if self.error_body is not None:
            exc.error_body = self.error_body
        raise exc
        if False:  # pragma: no cover
            yield ""


def _build_ttft_app(model_id: str, adapter: BaseAdapter, mock_log_store, monkeypatch) -> FastAPI:
    """Build a minimal app for TTFT testing."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    router.register_route(model_id, [(adapter, 1.0)])

    app = FastAPI(title="TTFT Test")
    app.state.services = AppServices(
        router=router,
        db_logger=None,
        log_store=mock_log_store,
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    return app


async def _get_db_log_ttft(mock_log_store, timeout: float = 2.0) -> tuple[bool, int | None]:
    """Wait for the background DB log task and return (logged, ttft_ms).

    Returns:
        (True, ttft_ms) if log_request was called — ttft_ms may be None.
        (False, None) if log_request was never called within timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock_log_store.log_request.call_count > 0:
            kwargs = mock_log_store.log_request.call_args.kwargs
            return True, kwargs.get("ttft_ms")
        await asyncio.sleep(0.05)
    return False, None


async def _wait_for_db_log_kwargs(mock_log_store, timeout: float = 2.0) -> dict[str, Any] | None:
    """Wait for the background DB log task and return kwargs."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock_log_store.log_request.call_count > 0:
            return mock_log_store.log_request.call_args.kwargs
        await asyncio.sleep(0.05)
    return None


@pytest.mark.asyncio
async def test_ttft_recorded_for_reasoning_content(monkeypatch, mock_log_store):
    """Streaming request where first delta has only reasoning_content should record ttft_ms."""
    app = _build_ttft_app(
        "deepseek-r1",
        ReasoningOnlyAdapter(_mk_cfg("deepseek-r1")),
        mock_log_store,
        monkeypatch,
    )

    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "deepseek-r1",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        assert resp.status_code == 200
        # Consume the stream fully
        async for _ in resp.aiter_lines():
            pass

    logged, ttft = await _get_db_log_ttft(mock_log_store)
    assert logged, "DB log_request should have been called"
    assert ttft is not None, "ttft_ms should be recorded when reasoning_content is in first delta"
    assert ttft >= 0


@pytest.mark.asyncio
async def test_ttft_recorded_for_openrouter_reasoning(monkeypatch, mock_log_store):
    """Streaming request where first delta has only reasoning should record ttft_ms."""
    app = _build_ttft_app(
        "minimax-m2.5",
        OpenRouterReasoningOnlyAdapter(_mk_cfg("minimax-m2.5")),
        mock_log_store,
        monkeypatch,
    )

    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "minimax-m2.5",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        assert resp.status_code == 200
        async for _ in resp.aiter_lines():
            pass

    logged, ttft = await _get_db_log_ttft(mock_log_store)
    assert logged, "DB log_request should have been called"
    assert ttft is not None, "ttft_ms should be recorded when reasoning is in first delta"
    assert ttft >= 0


@pytest.mark.asyncio
async def test_keepalive_emitted_without_cancelling_upstream(monkeypatch, mock_log_store):
    """A long gap before the first chunk should emit keepalive comments and still deliver output."""
    app = _build_ttft_app(
        "slow-start",
        SlowStartAdapter(_mk_cfg("slow-start"), delay_s=0.05),
        mock_log_store,
        monkeypatch,
    )

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, timeout=None):
        shortened = 0.01 if timeout is not None and timeout > 0.01 else timeout
        return await real_wait_for(awaitable, timeout=shortened)

    # Keepalive lives in StreamSession (completions_stream); patch its asyncio.
    from serving.servers.routers import completions_stream

    monkeypatch.setattr(completions_stream.asyncio, "wait_for", fast_wait_for)

    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "slow-start",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines()]

    assert any(line == ": keepalive" for line in lines), "expected an SSE keepalive comment"
    data_lines = [line for line in lines if line.startswith("data: ")]
    content = "".join(
        json.loads(line[6:])["choices"][0]["delta"].get("content", "")
        for line in data_lines
        if line != "data: [DONE]" and line != "data: {}"
    )
    assert content == "late"


@pytest.mark.asyncio
async def test_tool_calls_only_stream_is_not_classified_as_empty(
    monkeypatch, mock_log_store, caplog
):
    """Tool-calls-only streams are valid output and should not trigger empty-output warnings."""
    app = _build_ttft_app(
        "tool-only",
        ToolCallsOnlyAdapter(_mk_cfg("tool-only")),
        mock_log_store,
        monkeypatch,
    )

    transport = ASGITransport(app=app)
    with caplog.at_level(logging.WARNING):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "tool-only",
                    "messages": [{"role": "user", "content": "Inspect this file"}],
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200
                lines = [line async for line in resp.aiter_lines() if line.startswith("data: ")]

    tool_deltas = []
    for line in lines:
        if line == "data: [DONE]" or line == "data: {}":
            continue
        payload = json.loads(line[6:])
        if payload.get("choices"):
            delta = payload["choices"][0].get("delta", {})
            if delta.get("tool_calls"):
                tool_deltas.extend(delta["tool_calls"])

    assert tool_deltas, "expected at least one streamed tool call delta"
    assert not any(
        "no visible content or tool_calls" in record.getMessage() for record in caplog.records
    )

    db_kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert db_kwargs is not None, "expected background DB logging to run"
    response = db_kwargs["response"]
    assert response["choices"][0]["message"]["content"] is None
    assert response["choices"][0]["message"]["tool_calls"]
    assert response["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_true_empty_terminal_stream_logs_warning_and_db_empty_response(
    monkeypatch, mock_log_store, caplog
):
    """Streams that end without content or tool calls should be classified as true empty output."""
    app = _build_ttft_app(
        "empty-terminal",
        TrueEmptyTerminalAdapter(_mk_cfg("empty-terminal")),
        mock_log_store,
        monkeypatch,
    )

    transport = ASGITransport(app=app)
    with caplog.at_level(logging.WARNING):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "empty-terminal",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
                headers={"X-Reasoning-Passthrough": "true"},
            ) as resp:
                assert resp.status_code == 200
                lines = [line async for line in resp.aiter_lines() if line.startswith("data: ")]

    assert any(
        "no visible content or tool_calls" in record.getMessage() for record in caplog.records
    )
    # In passthrough mode, reasoning_content from upstream is visible
    assert any(
        "reasoning_content" in json.loads(line[6:])["choices"][0].get("delta", {})
        for line in lines
        if line not in {"data: [DONE]", "data: {}"} and json.loads(line[6:]).get("choices")
    )

    db_kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert db_kwargs is not None, "expected background DB logging to run"
    response = db_kwargs["response"]
    assert response["choices"][0]["message"]["content"] is None
    assert "tool_calls" not in response["choices"][0]["message"]


@pytest.mark.asyncio
async def test_ttft_preserved_in_error_path(monkeypatch, mock_log_store):
    """If TTFT was recorded before stream error, error-path DB log should include it."""
    app = _build_ttft_app(
        "error-model",
        ErrorAfterFirstTokenAdapter(_mk_cfg("error-model")),
        mock_log_store,
        monkeypatch,
    )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "error-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        # Consume stream (may get partial content then error)
        async for _ in resp.aiter_lines():
            pass

    logged, ttft = await _get_db_log_ttft(mock_log_store)
    assert logged, "DB log_request should have been called on error path"
    assert ttft is not None, "ttft_ms should be preserved in error-path DB log"
    assert ttft >= 0


@pytest.mark.asyncio
async def test_ttft_null_when_error_before_any_token(monkeypatch, mock_log_store):
    """If error occurs before any meaningful delta, ttft_ms should be None in DB log."""
    app = _build_ttft_app(
        "fail-model",
        ErrorBeforeAnyTokenAdapter(_mk_cfg("fail-model")),
        mock_log_store,
        monkeypatch,
    )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "fail-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        async for _ in resp.aiter_lines():
            pass

    logged, ttft = await _get_db_log_ttft(mock_log_store)
    assert logged, "DB log_request should have been called on error path"
    assert ttft is None, "ttft_ms should be None when error occurs before any meaningful delta"


@pytest.mark.asyncio
async def test_streaming_upstream_error_status_logged_to_db(monkeypatch, mock_log_store):
    """Streaming upstream HTTP errors should log the upstream status, not a generic 500."""
    app = _build_ttft_app(
        "upstream-status-model",
        UpstreamStatusErrorAdapter(_mk_cfg("upstream-status-model"), status_code=503),
        mock_log_store,
        monkeypatch,
    )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "upstream-status-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line.startswith("data: ")]

    db_kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert db_kwargs is not None, "DB log_request should have been called on error path"
    assert db_kwargs["status_code"] == 503

    error_chunks = [json.loads(line[6:])["error"] for line in lines if "error" in line]
    assert error_chunks
    assert error_chunks[0]["code"] == 503


@pytest.mark.asyncio
async def test_streaming_upstream_error_body_logged_to_db(monkeypatch, mock_log_store):
    """Streaming upstream HTTP errors should persist the upstream body for operators."""
    upstream_body = (
        '{"error":{"message":"cliproxy queue overloaded",'
        '"type":"server_error","api_key":"sk-secret"}}'
    )
    app = _build_ttft_app(
        "upstream-body-model",
        UpstreamStatusErrorAdapter(
            _mk_cfg("upstream-body-model"), status_code=503, error_body=upstream_body
        ),
        mock_log_store,
        monkeypatch,
    )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "upstream-body-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp,
    ):
        assert resp.status_code == 200
        async for _ in resp.aiter_lines():
            pass

    db_kwargs = await _wait_for_db_log_kwargs(mock_log_store)
    assert db_kwargs is not None, "DB log_request should have been called on error path"
    assert "cliproxy queue overloaded" in db_kwargs["error"]
    assert "upstream_body=" in db_kwargs["error"]
    assert "sk-secret" not in db_kwargs["error"]


# ===========================================================================
# X-Route-Pin integration tests
# ===========================================================================


class _ProviderEchoAdapter(DummyAdapter):
    """Expose the selected provider in response content for pin assertions."""

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        return self.format_response(content=self.config.provider, model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        provider = self.config.provider
        yield self.format_stream_chunk(model=self.config.id, content=provider)
        yield make_final_usage_chunk(
            model=self.config.id,
            messages=messages,
            total_content=provider,
        )
        yield done_sentinel()


class _LegacyCustomRouter:
    """One-release custom strategy shape that accepts only arbitrary params."""

    def __init__(self) -> None:
        self.adapter = _ProviderEchoAdapter(
            ModelConfig(
                id="test-model",
                name="test-model",
                provider="custom",
                base_url="http://custom",
            )
        )
        self.chat_params: list[dict[str, Any]] = []
        self.stream_params: list[dict[str, Any]] = []

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        self.chat_params.append(dict(params))
        return await self.adapter.chat_completion(messages, **params)

    async def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        self.stream_params.append(dict(params))
        async for chunk in self.adapter.stream_chat_completion(messages, **params):
            yield chunk

    def record_observation(self, _observation: Any) -> None:
        return None

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        return {}


@pytest.fixture
async def pin_app(monkeypatch, mock_db_logger, mock_log_store) -> FastAPI:
    """App with multi-provider routes for pin testing."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")  # all callers are admin

    router = RouteExecutor()
    zai = _ProviderEchoAdapter(_mk_cfg("test-model"))
    zai.config = ModelConfig(
        id="test-model",
        name="test-model",
        provider="zai",
        base_url="http://zai",
        context_length=8192,
        max_output_length=4096,
    )
    ollama = _ProviderEchoAdapter(_mk_cfg("test-model"))
    ollama.config = ModelConfig(
        id="test-model",
        name="test-model",
        provider="ollama",
        base_url="http://ollama",
        context_length=8192,
        max_output_length=4096,
    )
    disabled = _ProviderEchoAdapter(_mk_cfg("test-model"))
    disabled.config = ModelConfig(
        id="test-model",
        name="test-model",
        provider="featherless",
        base_url="http://featherless",
        context_length=8192,
        max_output_length=4096,
    )
    router.register_route("test-model", [(zai, 0.8), (ollama, 0.2), (disabled, 0.0)])

    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        db_logger=mock_db_logger,
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    return app


@pytest.fixture
async def pin_client(pin_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=pin_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _chat_body(model: str = "test-model", stream: bool = False) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}


@pytest.mark.asyncio
async def test_pin_nonstream_success(pin_client: AsyncClient):
    """Non-stream request pinned to a valid provider returns 200."""
    resp = await pin_client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"X-Route-Pin": "ollama"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "ollama"


@pytest.mark.asyncio
async def test_pin_stream_success(pin_client: AsyncClient):
    """Streaming request uses the unified router options call path."""
    async with pin_client.stream(
        "POST",
        "/v1/chat/completions",
        json=_chat_body(stream=True),
        headers={"X-Route-Pin": "ollama"},
    ) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines()]

    assert "data: [DONE]" in lines
    assert not any('"error"' in line for line in lines)
    assert _content_from_sse_lines(lines) == "ollama"


@pytest.mark.asyncio
async def test_pin_bypasses_model_router_registry(
    pin_client: AsyncClient,
    pin_app: FastAPI,
):
    """A provider pin must never dispatch through the per-model strategy."""
    routewise_router = MagicMock(name="routewise_router")
    registry = MagicMock()
    registry.get_router.return_value = routewise_router
    pin_app.state.services.model_router_registry = registry

    resp = await pin_client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"X-Route-Pin": "ollama"},
    )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ollama"
    registry.get_router.assert_not_called()
    routewise_router.chat_completion.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_unpinned_custom_strategy_does_not_receive_routing_options(
    pin_client: AsyncClient,
    pin_app: FastAPI,
    stream: bool,
):
    """Legacy custom strategies keep receiving only upstream request params."""
    custom_router = _LegacyCustomRouter()
    registry = MagicMock()
    registry.get_router.return_value = custom_router
    pin_app.state.services.model_router_registry = registry

    if stream:
        async with pin_client.stream(
            "POST",
            "/v1/chat/completions",
            json=_chat_body(stream=True),
        ) as resp:
            assert resp.status_code == 200
            lines = [line async for line in resp.aiter_lines()]
        assert "data: [DONE]" in lines
        assert _content_from_sse_lines(lines) == "custom"
        observed_params = custom_router.stream_params
    else:
        resp = await pin_client.post(
            "/v1/chat/completions",
            json=_chat_body(),
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "custom"
        observed_params = custom_router.chat_params

    registry.get_router.assert_called_once_with("test-model")
    assert observed_params
    assert all("routing_options" not in params for params in observed_params)


@pytest.mark.asyncio
async def test_pin_nonstream_miss_returns_400(pin_client: AsyncClient):
    """Non-stream request pinned to nonexistent provider returns 400."""
    resp = await pin_client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"X-Route-Pin": "nonexistent"},
    )
    assert resp.status_code == 400
    assert "nonexistent" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_pin_zero_weight_returns_400(pin_client: AsyncClient):
    """Pinning to a weight=0 (disabled) provider returns 400."""
    resp = await pin_client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"X-Route-Pin": "featherless"},
    )
    assert resp.status_code == 400
    assert "featherless" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_pin_stream_miss_returns_400(pin_client: AsyncClient):
    """Streaming request pinned to nonexistent provider returns 400, not 200+SSE error."""
    resp = await pin_client.post(
        "/v1/chat/completions",
        json=_chat_body(stream=True),
        headers={"X-Route-Pin": "nonexistent"},
    )
    # Must be 400, NOT 200 with an SSE error chunk
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_pin_stream_zero_weight_returns_400(pin_client: AsyncClient):
    """Streaming request pinned to weight=0 provider returns 400."""
    resp = await pin_client.post(
        "/v1/chat/completions",
        json=_chat_body(stream=True),
        headers={"X-Route-Pin": "featherless"},
    )
    assert resp.status_code == 400


# --- Image modality gate tests ---


@pytest.fixture
async def image_gate_app(monkeypatch, mock_db_logger, mock_log_store) -> FastAPI:
    """App with a text-only model to test image rejection."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    text_only_cfg = ModelConfig(
        id="text-model",
        name="Text Only",
        provider="zhipu",
        base_url="http://zhipu.test",
        input_modalities=["text"],
    )
    router.register_route("text-model", [(DummyAdapter(text_only_cfg), 1.0)])

    vision_cfg = ModelConfig(
        id="vision-model",
        name="Vision Model",
        provider="minimax",
        base_url="http://minimax.test",
        input_modalities=["text", "image"],
    )
    router.register_route("vision-model", [(DummyAdapter(vision_cfg), 1.0)])

    audio_cfg = ModelConfig(
        id="audio-model",
        name="Audio Model",
        provider="minimax",
        base_url="http://minimax.test",
        input_modalities=["text", "audio"],
    )
    router.register_route("audio-model", [(DummyAdapter(audio_cfg), 1.0)])

    video_cfg = ModelConfig(
        id="video-model",
        name="Video Model",
        provider="minimax",
        base_url="http://minimax.test",
        input_modalities=["text", "image", "video"],
    )
    router.register_route("video-model", [(DummyAdapter(video_cfg), 1.0)])

    # Model whose FIRST route is text-only but a later route accepts image. The
    # union gate must still admit image (route-aware dispatch then sends it to
    # the image-capable route, not the text-only primary).
    mixed_text = ModelConfig(
        id="mixed-model",
        name="Mixed Primary",
        provider="text-p",
        base_url="http://text.test",
        input_modalities=["text"],
    )
    mixed_vision = ModelConfig(
        id="mixed-model",
        name="Mixed Vision",
        provider="vis-p",
        base_url="http://vis.test",
        input_modalities=["text", "image"],
    )
    router.register_route(
        "mixed-model",
        [(DummyAdapter(mixed_text), 0.5), (DummyAdapter(mixed_vision), 0.5)],
    )

    app = FastAPI(title="Image Gate Test App")
    app.state.services = AppServices(
        router=router,
        db_logger=mock_db_logger,
        log_store=mock_log_store,
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    return app


@pytest.fixture
async def image_gate_client(image_gate_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=image_gate_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_image_rejected_for_text_only_model(image_gate_client: AsyncClient):
    """Sending image_url to a text-only model returns 400."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "text-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 400
    assert "does not support image" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_image_accepted_for_vision_model(image_gate_client: AsyncClient):
    """Sending image_url to a vision-capable model succeeds."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "vision-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_audio_rejected_for_text_only_model(image_gate_client: AsyncClient):
    """Sending input_audio to a text-only model returns 400."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "text-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this"},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "QUJD", "format": "wav"},
                        },
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 400
    assert "does not support audio" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_audio_accepted_for_audio_model(image_gate_client: AsyncClient):
    """Sending input_audio to an audio-capable model succeeds."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "audio-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this"},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "QUJD", "format": "wav"},
                        },
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_image_rejected_for_audio_only_model(image_gate_client: AsyncClient):
    """An audio-capable model still rejects image content it can't handle."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "audio-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image_url", "image_url": {"url": "https://x/img.png"}},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 400
    assert "does not support image" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_image_accepted_when_only_a_secondary_route_supports_it(
    image_gate_client: AsyncClient,
):
    """Union gate: image is admitted when ANY route supports it, not just the first."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "mixed-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_video_rejected_for_text_only_model(image_gate_client: AsyncClient):
    """Sending a video_url block to a text-only model returns 400."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "text-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Summarize this clip"},
                        {"type": "video_url", "video_url": {"url": "https://example.com/v.mp4"}},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 400
    assert "does not support video" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_video_accepted_for_video_model(image_gate_client: AsyncClient):
    """Sending a video_url block to a video-capable model succeeds."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "video-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Summarize this clip"},
                        {"type": "video_url", "video_url": {"url": "https://example.com/v.mp4"}},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_text_only_message_passes_text_only_model(image_gate_client: AsyncClient):
    """Text-only content on a text-only model succeeds."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "text-model",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_image_rejected_streaming_text_only_model(image_gate_client: AsyncClient):
    """Streaming request with image to text-only model returns 400."""
    resp = await image_gate_client.post(
        "/v1/chat/completions",
        json={
            "model": "text-model",
            "stream": True,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 400
