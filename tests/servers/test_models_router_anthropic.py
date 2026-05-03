"""Tests for /v1/models Anthropic-format detection and /anthropic/v1/models."""

from __future__ import annotations

import pytest


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
