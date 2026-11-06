"""A refused request's api_logs row is a bounded summary, on every path that writes one."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.observability import rejection_log
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions
from serving.servers.routers.completions_logging import CompletionsLogger
from serving.storage.bounded_log import (
    LOGGED_ERROR_MAX_CHARS,
    LOGGED_MODEL_ID_MAX_CHARS,
    REJECTED_LOG_MAX_CHARS,
)

_NOT_FOUND = re.compile(r"Model '.*' not found", re.DOTALL)


class _Adapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params):
        yield "data: [DONE]\n\n"


def _app(log_store: Any) -> FastAPI:
    router = FixedRouter()
    config = ModelConfig(id="real-model", name="real-model", provider="test", base_url="http://t")
    router.register_route("real-model", [(_Adapter(config), 1.0)])
    app = FastAPI()
    app.state.services = AppServices(router=router, log_store=log_store)  # type: ignore[attr-defined]
    install_error_handlers(app)
    app.include_router(completions.router)
    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "u1",
        "role": "free",
        "authenticated": True,
    }
    return app


async def _logged(log_store: Any, timeout: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_store.log_request.call_count:
            return log_store.log_request.call_args.kwargs
        await asyncio.sleep(0.02)
    raise AssertionError("no api_logs row was written")


@pytest.mark.asyncio
async def test_an_unknown_model_with_a_large_body_logs_a_bounded_row() -> None:
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    model = "no-such-model-" + "x" * 200_000
    messages = [{"role": "user", "content": f"{i} " + "word " * 2000} for i in range(200)]

    async with AsyncClient(transport=ASGITransport(app=_app(log_store)), base_url="http://t") as c:
        response = await c.post(
            "/v1/chat/completions",
            json={"model": model, "messages": messages, "max_tokens": 7},
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    row = await _logged(log_store)
    assert row["status_code"] == 404
    # About 2 MB were sent; the row keeps a bounded summary of it.
    assert len(repr(row["prompt"])) < 2 * REJECTED_LOG_MAX_CHARS
    assert row["prompt"][0]["content"] == messages[0]["content"]
    assert row["prompt"][-1]["content"].startswith("199 ")
    assert "messages" not in row["request_payload"]
    assert row["request_payload"]["max_tokens"] == 7
    assert len(row["model_id"]) == LOGGED_MODEL_ID_MAX_CHARS
    assert len(row["error"]) == LOGGED_ERROR_MAX_CHARS
    assert _NOT_FOUND.fullmatch(row["error"])
    assert row["metadata"]["payload_truncated"] is True


@pytest.mark.asyncio
async def test_a_served_request_keeps_its_whole_prompt() -> None:
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    logger = CompletionsLogger(log_store=log_store)
    prompt = [{"role": "user", "content": "y" * (4 * REJECTED_LOG_MAX_CHARS)}]
    payload = {"messages": prompt, "model": "real-model"}
    logger.schedule_log(
        "req-ok",
        {
            "request_id": "req-ok",
            "model_id": "real-model",
            "provider": "test",
            "prompt": prompt,
            "response": {"id": "x"},
            "usage": None,
            "latency_ms": 1,
            "status_code": 200,
            "request_payload": payload,
            "metadata": {},
        },
    )
    row = await _logged(log_store)
    await logger.stop()
    assert row["prompt"] is prompt
    assert row["request_payload"] is payload
    assert "payload_truncated" not in row["metadata"]


@pytest.mark.asyncio
async def test_a_gate_rejection_logs_a_bounded_prompt_and_model() -> None:
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    settings = MagicMock()
    settings.get_bool = AsyncMock(return_value=True)
    source = rejection_log.RejectionSource(
        route_path="/v1/messages",
        client_ip=lambda: "203.0.113.9",
        probe_marker=False,
        log_store=log_store,
        runtime_settings=settings,
    )
    model = "m" * 100_000
    messages = [{"role": "user", "content": "z" * 1000} for _ in range(1000)]

    await rejection_log.log_rejection_from(
        source=source,
        status_code=404,
        error_code="model_not_found",
        reason=f"Model '{model}' not found",
        user={"user_id": "u1", "role": "free", "authenticated": True},
        model_id=model,
        prompt=messages,
    )

    row = log_store.log_request.await_args.kwargs
    assert len(repr(row["prompt"])) < 2 * REJECTED_LOG_MAX_CHARS
    assert row["prompt"][0] == messages[0]
    assert len(row["model_id"]) == LOGGED_MODEL_ID_MAX_CHARS
    assert _NOT_FOUND.fullmatch(row["metadata"]["reason"])
    assert row["metadata"]["payload_truncated"] is True


@pytest.mark.asyncio
async def test_a_small_gate_rejection_is_logged_as_before() -> None:
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    settings = MagicMock()
    settings.get_bool = AsyncMock(return_value=True)
    source = rejection_log.RejectionSource(
        route_path="/v1/chat/completions",
        client_ip=lambda: "203.0.113.9",
        probe_marker=False,
        log_store=log_store,
        runtime_settings=settings,
    )
    prompt = [{"role": "user", "content": "hi"}]

    await rejection_log.log_rejection_from(
        source=source,
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="limit=3 role=free",
        user={"user_id": "u1", "role": "free", "authenticated": True},
        prompt=prompt,
    )

    row = log_store.log_request.await_args.kwargs
    assert row["prompt"] == prompt
    assert row["metadata"]["reason"] == "limit=3 role=free"
    assert "payload_truncated" not in row["metadata"]
