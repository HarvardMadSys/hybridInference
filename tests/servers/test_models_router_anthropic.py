"""Tests for /v1/models Anthropic-format detection and /anthropic/v1/models."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.executor import RouteExecutor
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.auth import optional_verify_api_key
from serving.servers.deps import AppServices
from serving.servers.routers import models


class _Adapter(BaseAdapter):
    async def chat_completion(self, messages, **params):
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _VisibilityResolver:
    def __init__(self, overrides: dict[str, str]):
        self._overrides = overrides

    async def get_effective_required_role(self, model_id: str, default_role: str) -> str:
        return self._overrides.get(model_id, default_role)


def _cfg(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="test",
        base_url="http://test",
        context_length=8192,
        max_output_length=4096,
        supported_params=["temperature", "top_p", "max_tokens"],
        input_modalities=["text"],
        output_modalities=["text"],
        quantization="bf16",
    )


@pytest.mark.asyncio
async def test_v1_models_returns_openai_format_by_default(test_client):
    """Plain /v1/models returns OpenAI/OpenRouter shape when no Anthropic headers."""
    r = await test_client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    # OpenAI/OpenRouter shape
    assert "data" in body
    if body["data"]:
        item = body["data"][0]
        # OpenAI format does not have a "type" field set to "model" on items.
        assert item.get("type") != "model"


@pytest.mark.asyncio
async def test_v1_models_with_anthropic_version_header_returns_anthropic_format(test_client):
    """anthropic-version header triggers Anthropic response shape on /v1/models."""
    r = await test_client.get("/v1/models", headers={"anthropic-version": "2023-06-01"})
    assert r.status_code == 200
    body = r.json()
    assert "data" in body
    assert "has_more" in body
    assert "first_id" in body
    assert "last_id" in body
    if body["data"]:
        item = body["data"][0]
        assert item["type"] == "model"
        assert "display_name" in item
        assert "created_at" in item


@pytest.mark.asyncio
async def test_v1_models_with_claude_cli_user_agent_returns_anthropic_format(test_client):
    """claude-cli User-Agent triggers Anthropic response shape on /v1/models."""
    r = await test_client.get("/v1/models", headers={"user-agent": "claude-cli/2.0"})
    assert r.status_code == 200
    body = r.json()
    if body["data"]:
        assert body["data"][0]["type"] == "model"


@pytest.mark.asyncio
async def test_v1_models_with_anthropic_sdk_user_agent_returns_anthropic_format(test_client):
    """anthropic-python User-Agent triggers Anthropic response shape on /v1/models."""
    r = await test_client.get("/v1/models", headers={"user-agent": "anthropic-python/0.42.0"})
    assert r.status_code == 200
    body = r.json()
    if body["data"]:
        assert body["data"][0]["type"] == "model"


@pytest.mark.asyncio
async def test_anthropic_v1_models_always_anthropic_format(test_client):
    """/anthropic/v1/models always returns the Anthropic list shape."""
    r = await test_client.get("/anthropic/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert "first_id" in body
    if body["data"]:
        assert body["data"][0]["type"] == "model"


@pytest.mark.asyncio
async def test_anthropic_model_listing_respects_runtime_visibility_override():
    router = RouteExecutor()
    visible = _Adapter(_cfg("visible-model"))
    hidden = _Adapter(_cfg("runtime-hidden-model"))
    router.register_route("visible-model", [(visible, 1.0)])
    router.register_route("runtime-hidden-model", [(hidden, 1.0)])

    app = FastAPI()
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=None,
        model_visibility_resolver=_VisibilityResolver({"runtime-hidden-model": "admin"}),
    )
    app.dependency_overrides[optional_verify_api_key] = lambda: {
        "authenticated": True,
        "role": "free",
        "user_id": "free-user",
    }
    app.include_router(models.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/anthropic/v1/models")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert "visible-model" in ids
    assert "runtime-hidden-model" not in ids
