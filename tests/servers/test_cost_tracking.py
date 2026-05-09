"""Integration tests for cost logging."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.observability.tracked_tasks import _TRACKED_TASKS
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions

SAMPLE_PRICING = {
    "prompt": "0.15",
    "completion": "1.25",
    "input_cache_reads": "0.01",
    "input_cache_writes": "0.02",
    "image": "0",
    "request": "0",
}


class TrackingAdapter(BaseAdapter):
    """Adapter that returns deterministic responses for testing."""

    def __init__(self, config: ModelConfig, stream_usage: dict[str, int]) -> None:
        super().__init__(config)
        self._stream_usage = stream_usage

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        """Return a deterministic non-streaming response with usage."""
        usage = UsageInfo(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cache_read_tokens=20,
        )
        return self.format_response(
            content="Test response",
            model=self.config.id,
            usage=usage,
        )

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params) -> Any:
        """Yield deterministic streaming chunks with final usage."""
        yield self.format_stream_chunk("Streamed", self.config.id)
        chunk = {
            "id": "stream-usage",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": self.config.id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": self._stream_usage,
            "_routing": {
                "provider": self.config.provider,
                "base_url": self.config.base_url,
            },
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"


@pytest.fixture(autouse=True)
def disable_auth_for_cost_tracking_tests(monkeypatch):
    """Disable auth for cost tracking tests; auth has independent coverage."""
    monkeypatch.setattr("serving.servers.auth.is_user_auth_enabled", lambda: False)


@pytest.fixture
async def tracking_app(monkeypatch, mock_db_logger) -> FastAPI:
    """Build a minimal completions app for cost logging assertions."""
    # Disable auth for cost tracking tests; auth has independent coverage.
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")

    router = RouteExecutor()
    config = ModelConfig(
        id="tracked-model",
        name="Tracked Model",
        provider="gemini",
        base_url="https://gemini.test",
        pricing=SAMPLE_PRICING,
    )
    adapter = TrackingAdapter(
        config=config,
        stream_usage={
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
        },
    )
    router.register_route("tracked-model", [(adapter, 1.0)])

    mock_log_store = MagicMock()
    mock_log_store.log_request = AsyncMock()
    mock_log_store.get_user_cost_today = AsyncMock(return_value=0.0)

    app = FastAPI(title="Cost Tracking App")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=mock_db_logger,
        log_store=mock_log_store,
    )

    async def fake_verify_api_key() -> dict[str, Any]:
        return {
            "user_id": "test-user",
            "role": "admin",
            "authenticated": True,
            "is_admin": True,
        }

    async def fake_concurrency_slot() -> None:
        return None

    app.dependency_overrides[verify_api_key] = fake_verify_api_key
    app.dependency_overrides[enforce_user_concurrency] = fake_concurrency_slot

    install_error_handlers(app)
    app.include_router(completions.router)
    yield app


@pytest.fixture
async def tracking_client(tracking_app: FastAPI):
    """Return an ASGI client bound to the tracking test app."""
    transport = ASGITransport(app=tracking_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, tracking_app


async def _drain_background_logs(app: FastAPI | None = None) -> None:
    tasks: list[asyncio.Task[Any]] = list(_TRACKED_TASKS)
    if app is not None:
        cl = getattr(app.state.services, "completions_logger", None)
        if cl is not None:
            tasks.extend(cl._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_non_streaming_logs_pricing_and_usage(tracking_client):
    """Assert non-streaming completions log pricing and usage."""
    client, app = tracking_client
    log_store = app.state.services.log_store
    log_store.log_request.reset_mock()

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "tracked-model",
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )

    assert response.status_code == 200
    await _drain_background_logs(app)
    log_store.log_request.assert_awaited_once()
    call = log_store.log_request.await_args
    kwargs = call.kwargs
    assert kwargs["provider"] == "gemini"
    assert kwargs["pricing"]["prompt"] == "0.15"
    assert kwargs["usage"]["prompt_tokens"] == 100
    assert kwargs["usage"]["completion_tokens"] == 50


@pytest.mark.asyncio
async def test_streaming_logs_usage(tracking_client):
    """Assert streaming completions log usage after consumption."""
    client, app = tracking_client
    log_store = app.state.services.log_store
    log_store.log_request.reset_mock()

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "tracked-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        async for _line in resp.aiter_lines():
            pass

    await _drain_background_logs(app)
    log_store.log_request.assert_awaited_once()
    kwargs = log_store.log_request.await_args.kwargs
    assert kwargs["usage"]["completion_tokens"] == 30
    assert kwargs["usage"]["prompt_tokens"] == 120


# Rate-limiter priority tests removed: the rate-limiter subsystem was
# deleted on dev (commit 1f5ca54). The mock_rate_limiter fixture and the
# acquire_tokens(priority=...) call no longer exist.
