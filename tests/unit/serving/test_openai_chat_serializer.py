"""Unit tests for OpenAI chat stream serializer.

Explicit tests for the public /v1/chat/completions API contract:
- Default passthrough mode: reasoning_content visible to clients
- X-Reasoning-Passthrough: false enables strict mode (no reasoning_content)
"""

from __future__ import annotations

import copy

import pytest

from serving import openai_chat_serializer as serializer
from serving.openai_chat_serializer import (
    SerializerMode,
    resolve_mode,
    sanitize_chunk,
    sanitize_response,
)


@pytest.fixture
def private_fields(monkeypatch):
    monkeypatch.setattr(serializer, "_private_response_fields", frozenset())
    serializer.register_private_response_fields("proxyTrace")


@pytest.mark.parametrize("mode", list(SerializerMode))
def test_unregistered_fields_are_unchanged(monkeypatch, mode):
    monkeypatch.setattr(serializer, "_private_response_fields", frozenset())
    payload = {"choices": [], "proxyTrace": {"spanId": "public-extension"}}
    assert sanitize_response(copy.deepcopy(payload), mode).response_json == payload
    result = sanitize_chunk(copy.deepcopy(payload), mode)
    assert result.should_forward and result.chunk_json == payload


@pytest.mark.parametrize(
    "invalid", ["", "field.path", 1, "choices", "usage", "model", "error", "_routing"]
)
def test_registration_rejects_invalid_batch_atomically(monkeypatch, invalid):
    monkeypatch.setattr(serializer, "_private_response_fields", frozenset({"alreadyPrivate"}))
    with pytest.raises(ValueError):
        serializer.register_private_response_fields("newPrivate", invalid)
    assert serializer._private_response_fields == frozenset({"alreadyPrivate"})


def test_registration_is_additive_and_idempotent(private_fields):
    serializer.register_private_response_fields("proxyTrace", "otherTrace")
    assert serializer._private_response_fields == frozenset({"proxyTrace", "otherTrace"})


@pytest.mark.parametrize("mode", list(SerializerMode))
@pytest.mark.parametrize("extra", [{"error": {"message": "unavailable"}}, {"extension": "keep"}])
def test_private_trace_does_not_discard_other_payload(private_fields, mode, extra):
    result = sanitize_chunk({"proxyTrace": None, **extra}, mode)
    assert result.should_forward and result.chunk_json == extra


@pytest.mark.parametrize("mode", list(SerializerMode))
def test_private_trace_only_envelope_is_not_published(private_fields, mode):
    result = sanitize_chunk(
        {"proxyTrace": {}, "id": "trace-1", "model": "example-model", "choices": [], "usage": None},
        mode,
    )
    assert not result.should_forward


@pytest.mark.unit
@pytest.mark.parametrize("mode", list(SerializerMode))
def test_upstream_trace_removed_from_normal_response_without_editing_payload(mode, private_fields):
    """Trace envelopes stay private while model content and usage remain intact."""
    message = {
        "role": "assistant",
        "content": 'The literal key "proxyTrace" is user-visible text.',
        "reasoning_content": "Reasoning remains controlled by the selected mode.",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"proxyTrace":"literal argument"}'},
            }
        ],
    }
    response = {
        "id": "response-1",
        "model": "example-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "proxyTrace": {"spanId": "private-upstream-span"},
        "_routing": {"provider": "private-provider"},
    }
    original = copy.deepcopy(response)
    result = sanitize_response(response, mode)
    expected = copy.deepcopy(original)
    expected.pop("proxyTrace")
    expected.pop("_routing")
    if mode == SerializerMode.STRICT_OPENAI:
        expected["choices"][0]["message"].pop("reasoning_content")
    assert result.response_json == expected
    assert result.routing_info == original["_routing"]
    assert response == original


@pytest.mark.unit
@pytest.mark.parametrize("mode", list(SerializerMode))
@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": None}]},
        {
            "choices": [
                {"index": 0, "delta": {"reasoning_content": "thinking"}, "finish_reason": None}
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"proxyTrace":"literal"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ],
)
def test_upstream_trace_does_not_change_stream_payload_or_mode_behavior(
    mode, payload, private_fields
):
    """Reasoning, tool deltas and terminal usage keep their existing semantics."""
    clean = {"id": "chunk-1", "model": "example-model", **copy.deepcopy(payload)}
    marked = copy.deepcopy(clean)
    marked["proxyTrace"] = {"spanId": "private-upstream-span"}
    assert sanitize_chunk(marked, mode) == sanitize_chunk(clean, mode)


@pytest.mark.unit
@pytest.mark.parametrize("mode", list(SerializerMode))
@pytest.mark.parametrize("marker", [{"spanId": "private-upstream-span"}, None])
def test_trace_only_stream_event_is_not_published(mode, marker, private_fields):
    """An upstream trace event must not turn into an empty public SSE event."""
    result = sanitize_chunk({"proxyTrace": marker}, mode)
    assert result.should_forward is False
    assert result.chunk_json is None


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
        "_routing": {"provider": "zai", "base_url": "https://api.z.ai"},
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.STRICT_OPENAI)
    assert "_routing" not in (result.chunk_json or {})
    assert result.routing_info == {"provider": "zai", "base_url": "https://api.z.ai"}
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
@pytest.mark.parametrize("field", ["reasoning", "thinking"])
def test_sanitize_strict_drops_alternate_reasoning_only_chunks(field: str):
    """Strict mode: alternate reasoning-only chunks are not forwarded."""
    chunk = {
        "id": "c1",
        "choices": [{"delta": {field: "Let me think..."}, "finish_reason": None}],
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
def test_sanitize_strict_removes_alternate_reasoning_from_mixed():
    """Strict mode: mixed chunks have alternate reasoning fields removed."""
    chunk = {
        "id": "c1",
        "choices": [
            {
                "delta": {
                    "content": "answer",
                    "reasoning": "because...",
                    "thinking": "also private...",
                }
            }
        ],
    }
    result = sanitize_chunk(dict(chunk), SerializerMode.STRICT_OPENAI)
    assert result.should_forward is True
    delta = result.chunk_json["choices"][0]["delta"]
    assert "reasoning" not in delta
    assert "thinking" not in delta
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
        "_routing": {"provider": "zai"},
    }
    result = sanitize_response(dict(response), SerializerMode.STRICT_OPENAI)
    message = result.response_json["choices"][0]["message"]
    assert "reasoning_content" not in message
    assert message["content"] == "answer"
    assert "_routing" not in result.response_json
    assert result.routing_info == {"provider": "zai"}


@pytest.mark.unit
def test_sanitize_response_strict_removes_alternate_reasoning_fields():
    """Strict mode: non-stream response should not expose alternate reasoning fields."""
    response = {
        "id": "resp-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning": "private chain of thought",
                    "thinking": "more private thought",
                }
            }
        ],
    }
    result = sanitize_response(dict(response), SerializerMode.STRICT_OPENAI)
    message = result.response_json["choices"][0]["message"]
    assert "reasoning" not in message
    assert "thinking" not in message
    assert message["content"] == "answer"


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
