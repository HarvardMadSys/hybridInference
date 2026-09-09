"""Tests for OpenAI-compatible embedding requests."""

from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest

from routing.executor import RouteExecutor
from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.servers.registry import register_from_models_yaml


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


@pytest.mark.parametrize(
    ("base_url", "embeddings_path", "expected"),
    [
        ("https://provider.example/v1", None, "https://provider.example/v1/embeddings"),
        ("https://provider.example/v1/", None, "https://provider.example/v1/embeddings"),
        ("https://provider.example", None, "https://provider.example/v1/embeddings"),
        ("https://provider.example/", None, "https://provider.example/v1/embeddings"),
        ("https://provider.example", "", "https://provider.example/v1/embeddings"),
        (
            "https://gateway.example/api/v2",
            None,
            "https://gateway.example/api/v2/v1/embeddings",
        ),
        (
            "https://gateway.example/api/v2",
            "/embeddings",
            "https://gateway.example/api/v2/embeddings",
        ),
        (
            "https://gateway.example/api/v2/",
            "embeddings",
            "https://gateway.example/api/v2/embeddings",
        ),
        (
            "https://provider.example/v1",
            "/custom/embeddings",
            "https://provider.example/v1/custom/embeddings",
        ),
    ],
)
def test_embeddings_url_honors_explicit_path(base_url, embeddings_path, expected):
    """Only an explicit override changes the historical URL inference."""
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id="embedding-test",
            name="Embedding test",
            provider="openai",
            base_url=base_url,
            embeddings_path=embeddings_path,
        )
    )

    assert adapter._build_embeddings_url() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("path_value", ["/embeddings", "embeddings", "${TEST_EMBEDDINGS_PATH}"])
async def test_yaml_embeddings_path_reaches_upstream(tmp_path, monkeypatch, path_value):
    """The YAML override must survive registration and actual request construction."""
    monkeypatch.setenv("TEST_EMBEDDINGS_PATH", "/embeddings")
    path = tmp_path / "models.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            models:
              - id: embedding-test
                name: Embedding test
                provider: openai_compat
                model_type: embedding
                provider_model_id: upstream-embedding
                route:
                  - kind: openai_compat
                    base_url: https://gateway.example/api/v2/
                    api_keys: [pool-key]
                    embeddings_path: {path_value}
                  - kind: vllm
                    base_url: http://localhost:8000/v1
            """
        )
    )
    embeddings = {}
    count, _ = register_from_models_yaml(RouteExecutor(), path, embeddings)
    assert count == 1
    primary, fallback = embeddings["embedding-test"]._adapters
    primary.http = MagicMock()
    primary.http.json_post = AsyncMock(return_value={"object": "list", "data": []})

    result = await embeddings["embedding-test"].embeddings(
        "hello", encoding_format="float", dimensions=3
    )

    assert result == {"object": "list", "data": []}
    primary.http.json_post.assert_awaited_once()
    call = primary.http.json_post.await_args.kwargs
    assert call["url"] == "https://gateway.example/api/v2/embeddings"
    assert call["headers"]["Authorization"] == "Bearer pool-key"
    assert call["json"] == {
        "model": "upstream-embedding",
        "input": "hello",
        "encoding_format": "float",
        "dimensions": 3,
    }
    assert fallback.config.embeddings_path is None
    assert fallback._build_embeddings_url() == "http://localhost:8000/v1/embeddings"


@pytest.mark.parametrize("path_value", ["null", '""'])
def test_empty_yaml_embeddings_path_keeps_default(tmp_path, path_value):
    """A null or empty YAML value must not become a literal endpoint suffix."""
    path = tmp_path / "models.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            models:
              - id: embedding-test
                name: Embedding test
                provider: openai_compat
                model_type: embedding
                route:
                  - kind: openai_compat
                    base_url: https://provider.example/v1
                    embeddings_path: {path_value}
            """
        )
    )
    embeddings = {}
    register_from_models_yaml(RouteExecutor(), path, embeddings)

    assert (
        embeddings["embedding-test"]._build_embeddings_url()
        == "https://provider.example/v1/embeddings"
    )


@pytest.mark.parametrize("path_value", ["123", "false", "[/embeddings]"])
def test_invalid_yaml_embeddings_path_is_rejected(tmp_path, path_value):
    """Invalid path types fail at configuration load, before serving traffic."""
    path = tmp_path / "models.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            models:
              - id: embedding-test
                name: Embedding test
                provider: openai_compat
                route:
                  - kind: openai_compat
                    base_url: https://provider.example/v1
                    embeddings_path: {path_value}
            """
        )
    )

    with pytest.raises(ValueError, match="embeddings_path must be a string or null"):
        register_from_models_yaml(RouteExecutor(), path)
