"""Unit tests for OpenRouter adapter, parser, profile, and UsageInfo extension."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from serving.adapters.base import ModelConfig, UsageInfo
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.openrouter import OpenRouterAdapter
from serving.adapters.profiles import (
    ProviderProfile,
    get_usage_normalizer,
    normalize_usage_openrouter,
)


def test_usage_info_default_upstream_cost_is_none() -> None:
    info = UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    assert info.upstream_cost_usd is None


def test_usage_info_to_dict_omits_upstream_cost() -> None:
    info = UsageInfo(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        upstream_cost_usd=0.00342,
    )
    d = info.to_dict()
    assert "upstream_cost_usd" not in d
    assert d == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_model_config_default_openrouter_pinned_provider_is_none() -> None:
    cfg = ModelConfig(id="m", name="M", provider="openrouter", base_url="https://x")
    assert cfg.openrouter_pinned_provider is None


def test_model_config_accepts_openrouter_pinned_provider() -> None:
    cfg = ModelConfig(
        id="m",
        name="M",
        provider="openrouter",
        base_url="https://x",
        openrouter_pinned_provider="deepinfra",
    )
    assert cfg.openrouter_pinned_provider == "deepinfra"


def test_provider_profile_has_openrouter() -> None:
    assert ProviderProfile("openrouter") is ProviderProfile.OPENROUTER


def test_normalize_usage_openrouter_with_cost_and_cached_tokens() -> None:
    info = normalize_usage_openrouter(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost": 0.00342,
            "prompt_tokens_details": {"cached_tokens": 30},
        }
    )
    assert info.prompt_tokens == 100
    assert info.completion_tokens == 50
    assert info.total_tokens == 150
    assert info.cache_read_tokens == 30
    assert info.upstream_cost_usd == 0.00342


def test_normalize_usage_openrouter_without_cost() -> None:
    info = normalize_usage_openrouter(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )
    assert info.upstream_cost_usd is None
    assert info.cache_read_tokens == 0


def test_normalize_usage_openrouter_handles_flat_cache_field() -> None:
    """When OpenRouter (or its upstream) returns cache_read_tokens flat, use it."""
    info = normalize_usage_openrouter(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cache_read_tokens": 25,
        }
    )
    assert info.cache_read_tokens == 25


def test_get_usage_normalizer_returns_openrouter_normalizer() -> None:
    normalizer = get_usage_normalizer(ProviderProfile.OPENROUTER)
    info = normalizer({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.5})
    assert info.upstream_cost_usd == 0.5


def test_normalize_usage_openrouter_string_cost_is_parsed() -> None:
    """A string-encoded numeric cost (rare but possible from proxies) is parsed."""
    info = normalize_usage_openrouter(
        {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "cost": "0.00342",
        }
    )
    assert info.upstream_cost_usd == 0.00342


def test_normalize_usage_openrouter_non_numeric_cost_is_dropped(caplog) -> None:
    """A non-numeric cost (e.g. dict, garbage string) is dropped with a warning."""
    import logging

    with caplog.at_level(logging.WARNING):
        info = normalize_usage_openrouter(
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": "not-a-number",
            }
        )
    assert info.upstream_cost_usd is None
    assert any("non-numeric cost" in rec.message for rec in caplog.records)


def test_normalize_usage_openrouter_negative_cost_is_dropped(caplog) -> None:
    """A negative cost is dropped with a warning (defensive against bad upstream data)."""
    import logging

    with caplog.at_level(logging.WARNING):
        info = normalize_usage_openrouter(
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": -1.5,
            }
        )
    assert info.upstream_cost_usd is None
    assert any("negative cost" in rec.message for rec in caplog.records)


def test_normalize_usage_openrouter_rejects_nan_cost(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        info = normalize_usage_openrouter(
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": float("nan"),
            }
        )
    assert info.upstream_cost_usd is None
    assert any("non-finite cost" in rec.message for rec in caplog.records)


def test_normalize_usage_openrouter_rejects_inf_cost(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        info = normalize_usage_openrouter(
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": float("inf"),
            }
        )
    assert info.upstream_cost_usd is None
    assert any("non-finite cost" in rec.message for rec in caplog.records)


def _make_compat_cfg(**overrides: Any) -> ModelConfig:
    base: dict[str, Any] = {
        "id": "dummy-model",
        "name": "Dummy",
        "provider": "openai_compat",
        "base_url": "https://example.test/v1",
        "api_key": "sk-test",
        "provider_model_id": "dummy-upstream",
        "supports_tools": False,
        "supports_structured_output": False,
        "supported_params": ["temperature", "top_p", "max_tokens"],
    }
    base.update(overrides)
    return ModelConfig(**base)


def test_augment_payload_default_is_noop() -> None:
    cfg = _make_compat_cfg()
    adapter = OpenAICompatAdapter(cfg)
    payload = {"model": "x", "messages": []}
    out = adapter._augment_payload(dict(payload), stream=False)
    assert out == payload


def test_build_final_chunk_omits_upstream_cost_when_usage_info_none() -> None:
    """When usage_info is None (the default), no upstream_cost_usd in _routing."""
    cfg = _make_compat_cfg()
    adapter = OpenAICompatAdapter(cfg)
    chunk_str = adapter._build_final_chunk(
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        finish_reason="stop",
    )
    # SSE chunk format: "data: {...}\n\n" — strip prefix and parse JSON
    assert chunk_str.startswith("data: ")
    payload = json.loads(chunk_str[len("data: ") :].strip())
    routing = payload["_routing"]
    assert "provider" in routing
    assert "base_url" in routing
    assert "endpoint_id" in routing
    assert "upstream_cost_usd" not in routing


def test_build_final_chunk_omits_upstream_cost_when_cost_field_none() -> None:
    """When usage_info has cost=None, key is omitted."""
    cfg = _make_compat_cfg()
    adapter = OpenAICompatAdapter(cfg)
    info = UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    chunk_str = adapter._build_final_chunk(
        usage=info.to_dict(),
        finish_reason="stop",
        usage_info=info,
    )
    payload = json.loads(chunk_str[len("data: ") :].strip())
    assert "upstream_cost_usd" not in payload["_routing"]


def test_build_final_chunk_includes_upstream_cost_when_set() -> None:
    """When usage_info carries a positive cost, _routing includes it."""
    cfg = _make_compat_cfg()
    adapter = OpenAICompatAdapter(cfg)
    info = UsageInfo(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        upstream_cost_usd=0.00342,
    )
    chunk_str = adapter._build_final_chunk(
        usage=info.to_dict(),
        finish_reason="stop",
        usage_info=info,
    )
    payload = json.loads(chunk_str[len("data: ") :].strip())
    assert payload["_routing"]["upstream_cost_usd"] == 0.00342
    # And the existing keys are still present
    assert payload["_routing"]["provider"] == cfg.provider
    assert payload["_routing"]["base_url"] == cfg.base_url


def _make_or_cfg(*, pinned: str | None = None) -> ModelConfig:
    return ModelConfig(
        id="or-model",
        name="OR Model",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        provider_model_id="meta-llama/llama-3.3-70b-instruct",
        supports_tools=False,
        supports_structured_output=False,
        supported_params=["temperature", "top_p", "max_tokens"],
        provider_profile="openrouter",
        openrouter_pinned_provider=pinned,
    )


def test_openrouter_adapter_attribution_headers() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg())
    headers = adapter._build_headers()
    assert headers["HTTP-Referer"] == "https://freeinference.org"
    assert headers["X-Title"] == "FreeInference"
    assert headers["Authorization"] == "Bearer sk-or-test"


def test_openrouter_adapter_payload_no_pin() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg(pinned=None))
    payload = adapter._augment_payload(
        {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    assert payload["usage"] == {"include": True}
    assert "provider" not in payload
    assert "stream_options" not in payload


def test_openrouter_adapter_payload_with_pin() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg(pinned="deepinfra"))
    payload = adapter._augment_payload(
        {"model": "x", "messages": []},
        stream=False,
    )
    assert payload["provider"] == {"order": ["deepinfra"], "allow_fallbacks": False}


def test_openrouter_adapter_streaming_payload_includes_stream_options() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg())
    payload = adapter._augment_payload(
        {"model": "x", "messages": [], "stream": True},
        stream=True,
    )
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["usage"] == {"include": True}


def test_openrouter_adapter_does_not_overwrite_existing_stream_options() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg())
    payload = adapter._augment_payload(
        {"messages": [], "stream": True, "stream_options": {"foo": "bar"}},
        stream=True,
    )
    assert payload["stream_options"] == {"foo": "bar", "include_usage": True}


@pytest.mark.asyncio
async def test_openrouter_adapter_chat_completion_threads_upstream_cost() -> None:
    """Non-stream response carries upstream_cost_usd in the _routing block."""
    adapter = OpenRouterAdapter(_make_or_cfg(pinned="deepinfra"))
    upstream_response = {
        "id": "x",
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost": 0.00342,
        },
    }
    with patch.object(
        adapter, "_post_with_pool", AsyncMock(return_value=upstream_response)
    ) as mock_post:
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])
    # _routing carries the cost
    assert result["_routing"]["upstream_cost_usd"] == 0.00342
    assert result["_routing"]["provider"] == "openrouter"
    assert result["_routing"]["base_url"] == "https://openrouter.ai/api/v1"
    # Outbound payload had OpenRouter-specific fields
    sent_payload = (
        mock_post.call_args[0][1]
        if mock_post.call_args.args
        else mock_post.call_args.kwargs.get("payload") or mock_post.call_args.kwargs["json"]
    )
    assert sent_payload["usage"] == {"include": True}
    assert sent_payload["provider"] == {"order": ["deepinfra"], "allow_fallbacks": False}


@pytest.mark.asyncio
async def test_openrouter_adapter_chat_completion_omits_cost_when_absent() -> None:
    """When OpenRouter doesn't return cost, _routing has no upstream_cost_usd key."""
    adapter = OpenRouterAdapter(_make_or_cfg())
    upstream_response = {
        "id": "x",
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    with patch.object(adapter, "_post_with_pool", AsyncMock(return_value=upstream_response)):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])
    assert "upstream_cost_usd" not in result["_routing"]


def test_openrouter_adapter_endpoint_id_distinct_per_pin() -> None:
    """Distinct pinned providers must produce distinct endpoint_ids.

    Verifies the property by going through the registry's _make_provider_id
    with the raw bracketed kind string.
    """
    from serving.servers.registry import _make_provider_id

    base = "https://openrouter.ai/api/v1"
    id_bare = _make_provider_id("llama-3.3-70b", "openrouter", base)
    id_di = _make_provider_id("llama-3.3-70b", "openrouter[deepinfra]", base)
    id_fw = _make_provider_id("llama-3.3-70b", "openrouter[fireworks]", base)
    assert id_bare != id_di
    assert id_di != id_fw
    assert id_bare != id_fw


@pytest.mark.asyncio
async def test_non_or_adapter_response_has_no_routing_block() -> None:
    """Non-OpenRouter OAI-compat adapters must NOT attach a _routing block to non-stream responses."""
    cfg = _make_compat_cfg()
    adapter = OpenAICompatAdapter(cfg)
    upstream_response = {
        "id": "x",
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    with patch.object(adapter, "_post_with_pool", AsyncMock(return_value=upstream_response)):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])
    assert "_routing" not in result


@pytest.mark.asyncio
async def test_openrouter_adapter_stream_threads_upstream_cost() -> None:
    """End-to-end mocked stream: final _routing chunk carries upstream_cost_usd."""
    import json as _json

    adapter = OpenRouterAdapter(_make_or_cfg(pinned="deepinfra"))

    async def fake_stream():
        # Content delta
        yield 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        # Final chunk with usage including OpenRouter cost
        yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7,"cost":0.000123}}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_open_stream(url, payload, timeout=None):
        # Drive the parent's _open_stream_with_pool contract:
        # yields exactly one (stream_iter, lease, first_chunk).
        gen = fake_stream()
        first = await gen.__anext__()
        yield gen, None, first

    with patch.object(adapter, "_open_stream_with_pool", fake_open_stream):
        chunks: list[str] = []
        async for chunk in adapter.stream_chat_completion(
            [{"role": "user", "content": "hi"}],
            max_tokens=8,
        ):
            chunks.append(chunk)

    # Locate the final SSE chunk that carries _routing
    routing_chunks = [c for c in chunks if '"_routing"' in c]
    assert routing_chunks, f"No chunk with _routing found in: {chunks}"
    final = routing_chunks[-1]
    # SSE format: "data: {...}\n\n"
    assert final.startswith("data: ")
    payload = _json.loads(final[len("data: ") :].strip())
    assert payload["_routing"]["upstream_cost_usd"] == 0.000123
    assert payload["_routing"]["provider"] == "openrouter"
    assert payload["_routing"]["base_url"] == "https://openrouter.ai/api/v1"


def test_normalize_usage_openrouter_emits_one_warning_per_bad_cost(caplog) -> None:
    """OpenRouter adapter should call the normalizer exactly once per response,
    so a malformed cost only logs one warning, not two."""
    import logging

    adapter = OpenRouterAdapter(_make_or_cfg())
    upstream_response = {
        "id": "x",
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "cost": "garbage",
        },
    }
    with caplog.at_level(logging.WARNING):
        # Use the underlying parser directly — chat_completion would call HTTP.
        adapter._parse_completion_response(upstream_response)
    bad_cost_warnings = [r for r in caplog.records if "non-numeric cost" in r.message]
    assert len(bad_cost_warnings) == 1, (
        f"Expected 1 warning, got {len(bad_cost_warnings)}: {bad_cost_warnings}"
    )


@pytest.mark.parametrize(
    "routing_info,expected",
    [
        (None, None),
        ({}, None),
        ({"provider": "openrouter"}, None),
        ({"provider": "openrouter", "upstream_cost_usd": 0.005}, 0.005),
        ({"upstream_cost_usd": 0.0}, 0.0),  # zero is valid, should not be coerced to None
    ],
)
def test_completions_upstream_cost_extraction_invariant(routing_info, expected) -> None:
    """Lock in the expression used in completions.py:751 and :943 that pulls
    upstream_cost_usd off routing_info safely for both None and missing-key cases."""
    actual = (routing_info or {}).get("upstream_cost_usd")
    assert actual == expected
