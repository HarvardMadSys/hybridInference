"""Unit tests for /v1/embeddings request logging.

Verifies the embeddings endpoint logs to the shared ``api_logs`` store
(tagged ``request_type=embedding``) on both success and error paths, mirroring
the chat/completions logging contract that the dashboards read from.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_embedding_adapters,
    get_log_store,
)
from serving.servers.routers import embeddings


class _FakeAdapter:
    def __init__(self, *, response: dict[str, Any] | None = None, raises: Exception | None = None):
        self.config = SimpleNamespace(
            provider="fake-provider",
            pricing={"prompt": "1.0", "completion": "0"},
        )
        self._response = response
        self._raises = raises

    async def embeddings(self, input_data, **params):
        if self._raises is not None:
            raise self._raises
        return self._response


class _CapturingLogger:
    """Stand-in for ``CompletionsLogger`` that records scheduled payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def schedule_log(self, request_id: str, log_data: dict[str, Any]) -> None:
        self.calls.append((request_id, log_data))


def _build_app(adapter: _FakeAdapter, logger: _CapturingLogger) -> FastAPI:
    app = FastAPI()
    app.include_router(embeddings.router)

    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "user-emb",
        "authenticated": True,
    }
    app.dependency_overrides[enforce_user_concurrency] = lambda: None
    app.dependency_overrides[get_embedding_adapters] = lambda: {"emb-model": adapter}
    app.dependency_overrides[get_log_store] = lambda: object()  # truthy → logging enabled
    app.dependency_overrides[get_completions_logger] = lambda: logger
    return app


async def _post(app: FastAPI, payload: dict[str, Any]):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/v1/embeddings", json=payload)


@pytest.mark.asyncio
async def test_embeddings_success_is_logged():
    response = {
        "object": "list",
        "model": "emb-model",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }
    adapter = _FakeAdapter(response=response)
    logger = _CapturingLogger()
    app = _build_app(adapter, logger)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 200

    assert len(logger.calls) == 1
    request_id, log_data = logger.calls[0]
    assert request_id.startswith("emb_")
    assert log_data["model_id"] == "emb-model"
    assert log_data["provider"] == "fake-provider"
    assert log_data["status_code"] == 200
    assert log_data["usage"] == {"prompt_tokens": 5, "total_tokens": 5}
    assert log_data["metadata"]["request_type"] == "embedding"
    assert log_data["metadata"]["user_id"] == "user-emb"
    # Full vectors must not be persisted — only a compact summary.
    assert log_data["response"]["data_count"] == 1
    assert log_data["response"]["dimensions"] == 3
    assert "data" not in log_data["response"]  # raw vectors omitted


@pytest.mark.asyncio
async def test_embeddings_unknown_model_is_logged():
    adapter = _FakeAdapter(response={})
    logger = _CapturingLogger()
    app = _build_app(adapter, logger)

    resp = await _post(app, {"model": "does-not-exist", "input": "hello"})
    assert resp.status_code == 404

    assert len(logger.calls) == 1
    _, log_data = logger.calls[0]
    assert log_data["status_code"] == 404
    assert log_data["metadata"]["request_type"] == "embedding"
    assert log_data["error"]


@pytest.mark.asyncio
async def test_embeddings_adapter_error_is_logged():
    adapter = _FakeAdapter(raises=RuntimeError("boom"))
    logger = _CapturingLogger()
    app = _build_app(adapter, logger)

    resp = await _post(app, {"model": "emb-model", "input": "hello"})
    assert resp.status_code == 500

    assert len(logger.calls) == 1
    _, log_data = logger.calls[0]
    assert log_data["status_code"] == 500
    assert log_data["metadata"]["request_type"] == "embedding"
    assert "boom" in log_data["error"]
