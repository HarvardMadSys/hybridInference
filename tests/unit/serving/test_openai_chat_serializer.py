"""Unit tests for OpenAI chat stream serializer.

Explicit tests for the public /v1/chat/completions API contract:
- Default passthrough mode: reasoning_content visible to clients
- X-Reasoning-Passthrough: false enables strict mode (no reasoning_content)
"""

from __future__ import annotations

import pytest

from serving.openai_chat_serializer import (
    SerializerMode,
    resolve_mode,
    sanitize_chunk,
    sanitize_response,
)


@pytest.mark.unit
def test_resolve_mode_default_passthrough():
    """Default (no header) resolves to reasoning_passthrough."""
    from starlette.datastructures import Headers

    h = Headers({})
    assert resolve_mode(h) == SerializerMode.REASONING_PASSTHROUGH


@pytest.mark.unit
def test_resolve_mode_strict_false():
    """X-Reasoning-Passthrough: false enables strict mode."""
    from starlette.datastructures import Headers

    h = Headers({"x-reasoning-passthrough": "false"})
    assert resolve_mode(h) == SerializerMode.STRICT_OPENAI


@pytest.mark.unit
def test_resolve_mode_strict_accepted_falsy_values():
    """All documented falsy header values opt into strict mode (case-insensitive)."""
    from starlette.datastructures import Headers

    for val in ("False", "FALSE", "no", "0"):
        h = Headers({"x-reasoning-passthrough": val})
        assert resolve_mode(h) == SerializerMode.STRICT_OPENAI


@pytest.mark.unit
def test_resolve_mode_true_stays_passthrough():
    """X-Reasoning-Passthrough: true stays passthrough."""
    from starlette.datastructures import Headers

    h = Headers({"x-reasoning-passthrough": "true"})
    assert resolve_mode(h) == SerializerMode.REASONING_PASSTHROUGH


@pytest.mark.unit
def test_sanitize_always_strips_routing():
    """_routing is always stripped from output."""
    chunk = {
        "id": "c1",
        "choices": [{"delta": {"content": "hi"}}],
        "_routing": {"provider": "zhipu", "base_url": "https://api.z.ai"},
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.STRICT_OPENAI)
    assert "_routing" not in (result.chunk_json or {})
    assert result.routing_info == {"provider": "zhipu", "base_url": "https://api.z.ai"}
    assert result.should_forward is True


@pytest.mark.unit
def test_sanitize_strict_drops_reasoning_only_chunks():
    """Strict mode: reasoning-only chunks are not forwarded."""
    chunk = {
        "id": "c1",
        "choices": [{"delta": {"reasoning_content": "Let me think..."}, "finish_reason": None}],
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.STRICT_OPENAI)
    assert result.should_forward is False
    assert result.chunk_json is None


@pytest.mark.unit
def test_sanitize_strict_removes_reasoning_from_mixed():
    """Strict mode: mixed chunks have reasoning_content removed."""
    chunk = {
        "id": "c1",
        "choices": [{"delta": {"content": "answer", "reasoning_content": "because..."}}],
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.STRICT_OPENAI)
    assert result.should_forward is True
    delta = result.chunk_json["choices"][0]["delta"]
    assert "reasoning_content" not in delta
    assert delta["content"] == "answer"


@pytest.mark.unit
def test_sanitize_passthrough_preserves_reasoning():
    """Passthrough mode: reasoning_content is preserved."""
    chunk = {
        "id": "c1",
        "choices": [{"delta": {"reasoning_content": "thinking..."}}],
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.REASONING_PASSTHROUGH)
    assert result.should_forward is True
    delta = result.chunk_json["choices"][0]["delta"]
    assert delta["reasoning_content"] == "thinking..."


@pytest.mark.unit
def test_sanitize_extracts_usage_and_routing():
    """Usage and routing metadata are returned for completions.py accumulation."""
    chunk = {
        "id": "c1",
        "choices": [{"delta": {"content": "hi"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        "_routing": {"provider": "deepseek"},
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.STRICT_OPENAI)
    assert result.usage_data == {"prompt_tokens": 10, "completion_tokens": 1}
    assert result.routing_info == {"provider": "deepseek"}


@pytest.mark.unit
def test_sanitize_reasoning_with_tool_calls_stripped():
    """Strict mode: chunk with reasoning + tool_calls strips reasoning only."""
    chunk = {
        "id": "c1",
        "choices": [
            {
                "delta": {
                    "reasoning_content": "I'll call read_file",
                    "tool_calls": [{"index": 0, "function": {"name": "read_file"}}],
                }
            }
        ],
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.STRICT_OPENAI)
    assert result.should_forward is True
    delta = result.chunk_json["choices"][0]["delta"]
    assert "reasoning_content" not in delta
    assert delta["tool_calls"]


@pytest.mark.unit
def test_sanitize_suppresses_synthetic_routing_only_chunk():
    """Synthetic routing chunk (no choices, no usage) is not forwarded.

    FixedRouter.stream_chat_completion emits a metadata-only chunk at the
    start of every stream so completions.py can recover the upstream
    provider/base_url/endpoint_id. That chunk must be consumed internally,
    never reach the SSE client.
    """
    chunk = {
        "choices": [],
        "_routing": {"provider": "anthropic", "base_url": "https://api.anthropic.com"},
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.REASONING_PASSTHROUGH)
    assert result.should_forward is False
    assert result.chunk_json is None
    assert result.routing_info == {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
    }

    # Same in strict mode.
    result_strict = sanitize_chunk(dict(chunk), SerializerMode.STRICT_OPENAI)
    assert result_strict.should_forward is False
    assert result_strict.chunk_json is None


@pytest.mark.unit
def test_sanitize_keeps_empty_choices_chunk_when_usage_present():
    """An empty-choices chunk that carries usage is still forwarded.

    Some providers emit a final chunk with empty choices but populated
    usage. Suppressing it would lose the usage payload to clients.
    """
    chunk = {
        "choices": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.REASONING_PASSTHROUGH)
    assert result.should_forward is True
    assert result.usage_data == {"prompt_tokens": 10, "completion_tokens": 5}


@pytest.mark.unit
def test_sanitize_response_strict_removes_reasoning_content():
    """Strict mode: non-stream response should not expose reasoning_content."""
    response = {
        "id": "resp-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "private chain of thought",
                }
            }
        ],
        "_routing": {"provider": "zhipu"},
    }
    result = sanitize_response(dict(response), SerializerMode.STRICT_OPENAI)
    message = result.response_json["choices"][0]["message"]
    assert "reasoning_content" not in message
    assert message["content"] == "answer"
    assert "_routing" not in result.response_json
    assert result.routing_info == {"provider": "zhipu"}


@pytest.mark.unit
def test_sanitize_response_passthrough_preserves_reasoning_content():
    """Passthrough mode: non-stream response keeps reasoning_content."""
    response = {
        "id": "resp-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "thinking...",
                }
            }
        ],
    }
    result = sanitize_response(dict(response), SerializerMode.REASONING_PASSTHROUGH)
    message = result.response_json["choices"][0]["message"]
    assert message["reasoning_content"] == "thinking..."
