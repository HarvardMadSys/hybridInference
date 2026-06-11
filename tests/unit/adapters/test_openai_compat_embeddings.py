"""Tests for OpenAI-compatible embedding requests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter


@pytest.mark.asyncio
async def test_embeddings_use_selected_key_from_pool() -> None:
    """Embedding requests must authenticate when a route uses api_keys."""
    config = ModelConfig(
        id="bge-m3",
        name="BGE-M3",
        provider="sglang",
        base_url="http://proxy.local:8001/v1",
        api_keys=["pool-key"],
        provider_model_id="BAAI/bge-m3",
        model_type="embedding",
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    adapter.http.json_post = AsyncMock(return_value={"object": "list", "data": []})

    result = await adapter.embeddings("hello", encoding_format="float")

    assert result == {"object": "list", "data": []}
    adapter.http.json_post.assert_awaited_once()
    call = adapter.http.json_post.await_args.kwargs
    assert call["url"] == "http://proxy.local:8001/v1/embeddings"
    assert call["json"] == {
        "model": "BAAI/bge-m3",
        "input": "hello",
        "encoding_format": "float",
    }
    assert call["headers"]["Authorization"] == "Bearer pool-key"
