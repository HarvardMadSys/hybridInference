"""Unit tests for claude_format streaming helpers and ClaudeAdapter (Vertex) streaming.

Tests cover:
- handle_stream_event cache token extraction (Step 1 enhancement)
- ClaudeAdapter normal streaming (text, tool calls, usage)
- ClaudeAdapter Vertex "message" type one-shot response
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.claude import ClaudeAdapter
from serving.adapters.claude_format import (
    StreamEventResult,
    ToolCallAccumulator,
    build_final_usage,
    handle_stream_event,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vertex_config(**overrides) -> ModelConfig:
    defaults = {
        "id": "claude-sonnet-4-6",
        "name": "Claude Sonnet 4.6",
        "provider": "claude",
        "base_url": "https://vertex.example.com",
        "api_key": "test-api-key",
        "context_length": 200000,
        "max_output_length": 4096,
        "supports_tools": True,
    }
    defaults.update(overrides)
    return ModelConfig(**defaults)


def _make_vertex_adapter() -> ClaudeAdapter:
    """Create a ClaudeAdapter with mocked HTTP client."""
    config = _make_vertex_config()
    adapter = ClaudeAdapter(config)
    adapter.http = MagicMock()
    return adapter


def _vertex_stream_events(
    text: str = "Hello world",
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int = 0,
    cache_write: int = 0,
) -> list[str]:
    """Build Vertex streaming events (raw JSON lines, no 'data: ' prefix).

    Vertex uses mode="auto" which yields raw JSON, not SSE.
    """
    message_start_usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": 0,
    }
    if cache_read:
        message_start_usage["cache_read_input_tokens"] = cache_read
    if cache_write:
        message_start_usage["cache_creation_input_tokens"] = cache_write

    events = [
        json.dumps(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_vertex_1",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6-20250514",
                    "usage": message_start_usage,
                },
            }
        ),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        ),
    ]
    # Split text into word-level deltas
    for word in text.split():
        events.append(
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": word + " "},
                }
            )
        )
    events.extend(
        [
            json.dumps({"type": "content_block_stop", "index": 0}),
            json.dumps(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason},
                    "usage": {"output_tokens": output_tokens},
                }
            ),
            json.dumps({"type": "message_stop"}),
        ]
    )
    return events


def _vertex_stream_with_tools() -> list[str]:
    """Build Vertex streaming events with tool_use blocks."""
    return [
        json.dumps(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_vertex_1",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6-20250514",
                    "usage": {"input_tokens": 50, "output_tokens": 0},
                },
            }
        ),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Let me check."},
            }
        ),
        json.dumps({"type": "content_block_stop", "index": 0}),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_vertex_1",
                    "name": "get_weather",
                    "input": {},
                },
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'},
            }
        ),
        json.dumps({"type": "content_block_stop", "index": 1}),
        json.dumps(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 30},
            }
        ),
        json.dumps({"type": "message_stop"}),
    ]


def _vertex_message_response(
    text: str = "Complete response",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_write: int = 0,
    thinking_tokens: int = 0,
    tool_use: list[dict] | None = None,
    stop_reason: str = "end_turn",
) -> str:
    """Build a Vertex one-shot 'message' type response (raw JSON line)."""
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_use:
        content.extend(tool_use)

    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    if cache_write:
        usage["cache_creation_input_tokens"] = cache_write
    if thinking_tokens:
        usage["thinking_tokens"] = thinking_tokens

    return json.dumps(
        {
            "id": "msg_vertex_complete",
            "type": "message",
            "role": "assistant",
            "content": content,
            "stop_reason": stop_reason,
            "usage": usage,
        }
    )


# ===========================================================================
# Tests for handle_stream_event cache token enhancement
# ===========================================================================


class TestHandleStreamEventCacheTokens:
    """Test that handle_stream_event extracts cache tokens from message_start."""

    def test_message_start_with_cache_tokens(self):
        event = {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "usage": {
                    "input_tokens": 500,
                    "cache_read_input_tokens": 300,
                    "cache_creation_input_tokens": 100,
                },
            },
        }
        acc = ToolCallAccumulator()
        result = handle_stream_event(event, acc)

        assert result.input_tokens == 500
        assert result.cache_read_tokens == 300
        assert result.cache_write_tokens == 100

    def test_message_start_without_cache_tokens(self):
        event = {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "usage": {"input_tokens": 200},
            },
        }
        acc = ToolCallAccumulator()
        result = handle_stream_event(event, acc)

        assert result.input_tokens == 200
        assert result.cache_read_tokens == 0
        assert result.cache_write_tokens == 0

    def test_message_start_with_null_cache_tokens(self):
        """Handle None/null values in cache fields (some API versions)."""
        event = {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": None,
                    "cache_creation_input_tokens": None,
                },
            },
        }
        acc = ToolCallAccumulator()
        result = handle_stream_event(event, acc)

        assert result.input_tokens == 100
        assert result.cache_read_tokens == 0
        assert result.cache_write_tokens == 0

    def test_text_delta_no_cache_fields(self):
        """Non-message_start events should have default 0 cache tokens."""
        event = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hello"},
        }
        acc = ToolCallAccumulator()
        result = handle_stream_event(event, acc)

        assert result.text_delta == "hello"
        assert result.cache_read_tokens == 0
        assert result.cache_write_tokens == 0

    def test_message_delta_output_tokens(self):
        event = {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 42},
        }
        acc = ToolCallAccumulator()
        result = handle_stream_event(event, acc)

        assert result.output_tokens == 42
        assert result.finish_reason == "stop"

    def test_message_stop(self):
        event = {"type": "message_stop"}
        acc = ToolCallAccumulator()
        result = handle_stream_event(event, acc)

        assert result.is_done is True

    def test_unknown_event_returns_empty(self):
        event = {"type": "ping"}
        acc = ToolCallAccumulator()
        result = handle_stream_event(event, acc)

        assert result.text_delta is None
        assert result.input_tokens == 0
        assert result.is_done is False


class TestStreamEventResultDefaults:
    """Verify StreamEventResult default values for backwards compatibility."""

    def test_defaults(self):
        result = StreamEventResult()
        assert result.text_delta is None
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.cache_read_tokens == 0
        assert result.cache_write_tokens == 0
        assert result.finish_reason is None
        assert result.is_done is False


class TestToolCallAccumulator:
    """Test ToolCallAccumulator via handle_stream_event."""

    def test_single_tool(self):
        acc = ToolCallAccumulator()

        handle_stream_event(
            {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "id": "t1", "name": "fn1"},
            },
            acc,
        )
        handle_stream_event(
            {
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": '{"a":'},
            },
            acc,
        )
        handle_stream_event(
            {
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": "1}"},
            },
            acc,
        )
        handle_stream_event({"type": "content_block_stop"}, acc)

        tools = acc.get_completed()
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "fn1"
        assert tools[0]["function"]["arguments"] == '{"a":1}'
        assert tools[0]["index"] == 0
        assert tools[0]["id"] == "t1"

    def test_multiple_tools(self):
        acc = ToolCallAccumulator()

        # Tool 1
        handle_stream_event(
            {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "id": "t1", "name": "fn1"},
            },
            acc,
        )
        handle_stream_event(
            {
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
            acc,
        )
        handle_stream_event({"type": "content_block_stop"}, acc)

        # Tool 2
        handle_stream_event(
            {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "id": "t2", "name": "fn2"},
            },
            acc,
        )
        handle_stream_event(
            {
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": '{"x":2}'},
            },
            acc,
        )
        handle_stream_event({"type": "content_block_stop"}, acc)

        tools = acc.get_completed()
        assert len(tools) == 2
        assert tools[0]["index"] == 0
        assert tools[1]["index"] == 1
        assert tools[0]["function"]["name"] == "fn1"
        assert tools[1]["function"]["name"] == "fn2"


class TestBuildFinalUsage:
    def test_with_cache(self):
        usage = build_final_usage(
            input_tokens=500,
            output_tokens=100,
            cache_read_input_tokens=200,
            cache_creation_input_tokens=50,
        )
        # Cache-inclusive prompt_tokens: 500 + 200 + 50
        assert usage["prompt_tokens"] == 750
        assert usage["completion_tokens"] == 100
        assert usage["total_tokens"] == 850  # 750 + 100
        assert usage["cache_read_tokens"] == 200
        assert usage["cache_write_tokens"] == 50

    def test_without_cache(self):
        usage = build_final_usage(input_tokens=100, output_tokens=50)
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50
        assert usage["total_tokens"] == 150
        assert "cache_read_tokens" not in usage
        assert "cache_write_tokens" not in usage

    def test_explicit_zero_reported_emits_cache_read(self):
        # Provider reported cache_read_input_tokens: 0 (a real miss). It must be
        # preserved so downstream logging can distinguish it from "not reported".
        usage = build_final_usage(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_read_reported=True,
        )
        assert usage["cache_read_tokens"] == 0


# ===========================================================================
# Tests for ClaudeAdapter (Vertex) streaming
# ===========================================================================


class TestClaudeAdapterStream:
    """Test ClaudeAdapter.stream_chat_completion with normal streaming events."""

    @pytest.mark.asyncio
    async def test_basic_text_stream(self):
        adapter = _make_vertex_adapter()

        events = _vertex_stream_events(text="Hello world", input_tokens=100, output_tokens=20)

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "Hi"}]):
            chunks.append(chunk)

        # Should have text deltas + final usage + [DONE]
        assert len(chunks) >= 3

        # Last is [DONE]
        assert chunks[-1].strip() == "data: [DONE]"

        # Second-to-last has usage and _routing
        final = json.loads(chunks[-2][6:])
        assert "usage" in final
        assert final["usage"]["prompt_tokens"] == 100
        assert final["usage"]["completion_tokens"] == 20
        assert final["_routing"]["provider"] == "claude"

        # Text content present in earlier chunks
        text_content = ""
        for c in chunks[:-2]:
            if c.startswith("data: "):
                data = json.loads(c[6:])
                delta = data.get("choices", [{}])[0].get("delta", {})
                text_content += delta.get("content", "")
        assert "Hello" in text_content
        assert "world" in text_content

    @pytest.mark.asyncio
    async def test_stream_with_cache_tokens(self):
        """Verify cache tokens are included in final usage (bug fix validation)."""
        adapter = _make_vertex_adapter()

        events = _vertex_stream_events(
            text="Cached response",
            input_tokens=500,
            output_tokens=30,
            cache_read=200,
            cache_write=50,
        )

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        final = json.loads(chunks[-2][6:])
        usage = final["usage"]
        # Cache-inclusive prompt_tokens: 500 + 200 + 50
        assert usage["prompt_tokens"] == 750
        assert usage["completion_tokens"] == 30
        assert usage["cache_read_tokens"] == 200
        assert usage["cache_write_tokens"] == 50

    @pytest.mark.asyncio
    async def test_stream_with_tool_calls(self):
        adapter = _make_vertex_adapter()

        events = _vertex_stream_with_tools()

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion(
            [{"role": "user", "content": "weather?"}]
        ):
            chunks.append(chunk)

        # Find tool call chunk
        tool_chunk_found = False
        for c in chunks:
            if c.startswith("data: ") and c.strip() != "data: [DONE]":
                data = json.loads(c[6:])
                tc = data.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
                if tc:
                    tool_chunk_found = True
                    assert tc[0]["function"]["name"] == "get_weather"
                    assert '"city"' in tc[0]["function"]["arguments"]
                    assert tc[0]["id"] == "toolu_vertex_1"
                    assert tc[0]["index"] == 0
        assert tool_chunk_found

        # Final chunk has finish_reason=tool_calls
        final = json.loads(chunks[-2][6:])
        assert final["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.asyncio
    async def test_stream_finish_reason_length(self):
        adapter = _make_vertex_adapter()

        events = _vertex_stream_events(
            text="Truncated", stop_reason="max_tokens", output_tokens=4096
        )

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        final = json.loads(chunks[-2][6:])
        assert final["choices"][0]["finish_reason"] == "length"


# ===========================================================================
# Tests for Vertex "message" type one-shot response
# ===========================================================================


class TestClaudeAdapterVertexMessage:
    """Test ClaudeAdapter handling of Vertex's one-shot 'message' type response."""

    @pytest.mark.asyncio
    async def test_nonstream_upstream_error_raises(self):
        """Non-streaming Vertex API error payloads should raise."""
        adapter = _make_vertex_adapter()

        async def mock_json_post(*args, **kwargs):
            return {"Code": "10001", "Error": "Resource key unavailable"}

        adapter.http.json_post_with_retry = mock_json_post

        with pytest.raises(RuntimeError, match="Upstream API error: Resource key unavailable"):
            await adapter.chat_completion([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_message_type_text(self):
        adapter = _make_vertex_adapter()

        msg = _vertex_message_response(
            text="Complete text response",
            input_tokens=200,
            output_tokens=50,
        )

        async def mock_stream(*args, **kwargs):
            yield msg

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        # Should have text chunk + final usage + [DONE]
        assert chunks[-1].strip() == "data: [DONE]"

        # Text content
        text_found = False
        for c in chunks:
            if c.startswith("data: ") and c.strip() != "data: [DONE]":
                data = json.loads(c[6:])
                delta = data.get("choices", [{}])[0].get("delta", {})
                if delta.get("content") == "Complete text response":
                    text_found = True
        assert text_found

    @pytest.mark.asyncio
    async def test_message_type_with_cache_and_thinking(self):
        adapter = _make_vertex_adapter()

        msg = _vertex_message_response(
            text="Thoughtful response",
            input_tokens=1000,
            output_tokens=200,
            cache_read=500,
            cache_write=100,
            thinking_tokens=50,
        )

        async def mock_stream(*args, **kwargs):
            yield msg

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        # Final usage chunk (before [DONE])
        final = json.loads(chunks[-2][6:])
        usage = final["usage"]
        # Cache-inclusive prompt_tokens: 1000 + 500 + 100
        assert usage["prompt_tokens"] == 1600
        assert usage["completion_tokens"] == 200
        assert usage["cache_read_tokens"] == 500
        assert usage["cache_write_tokens"] == 100
        assert usage["reasoning_tokens"] == 50

    @pytest.mark.asyncio
    async def test_message_type_with_tools(self):
        adapter = _make_vertex_adapter()

        tool_use = [
            {
                "type": "tool_use",
                "id": "toolu_msg_1",
                "name": "search",
                "input": {"query": "test"},
            }
        ]
        msg = _vertex_message_response(
            text="Let me search.",
            tool_use=tool_use,
            stop_reason="tool_use",
            input_tokens=100,
            output_tokens=40,
        )

        async def mock_stream(*args, **kwargs):
            yield msg

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion(
            [{"role": "user", "content": "search something"}]
        ):
            chunks.append(chunk)

        # Should have tool calls
        tool_found = False
        for c in chunks:
            if c.startswith("data: ") and c.strip() != "data: [DONE]":
                data = json.loads(c[6:])
                tc = data.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
                if tc:
                    tool_found = True
                    assert tc[0]["function"]["name"] == "search"
                    assert tc[0]["id"] == "toolu_msg_1"
        assert tool_found

        # Final chunk has finish_reason=tool_calls
        final = json.loads(chunks[-2][6:])
        assert final["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.asyncio
    async def test_upstream_error_raises(self):
        """Vertex upstream API error (Code+Error fields) should raise."""
        adapter = _make_vertex_adapter()

        error_response = json.dumps({"Code": "500", "Error": "Internal server error"})

        async def mock_stream(*args, **kwargs):
            yield error_response

        adapter.http.stream_post = mock_stream

        with pytest.raises(RuntimeError, match="Upstream API error"):
            async for _ in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
                pass


# ===========================================================================
# Edge case tests
# ===========================================================================


class TestClaudeAdapterEdgeCases:
    """Edge cases that could break the refactored streaming logic."""

    @pytest.mark.asyncio
    async def test_empty_stream_no_content(self):
        """Vertex returns message_start → message_stop with no content blocks."""
        adapter = _make_vertex_adapter()

        events = [
            json.dumps(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_empty",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6-20250514",
                        "usage": {"input_tokens": 50, "output_tokens": 0},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 0},
                }
            ),
            json.dumps({"type": "message_stop"}),
        ]

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        # Should still emit final usage + [DONE] without crashing
        assert chunks[-1].strip() == "data: [DONE]"
        final = json.loads(chunks[-2][6:])
        assert "usage" in final
        assert final["usage"]["prompt_tokens"] == 50
        assert final["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_json_decode_error_mid_stream(self):
        """A corrupted JSON line mid-stream should be skipped, not crash."""
        adapter = _make_vertex_adapter()

        events = [
            json.dumps(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6-20250514",
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            "this is not valid json {{{",  # corrupted line
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                }
            ),
            json.dumps({"type": "content_block_stop", "index": 0}),
            json.dumps(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 5},
                }
            ),
            json.dumps({"type": "message_stop"}),
        ]

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        # Should complete successfully with the text that came after the bad line
        assert chunks[-1].strip() == "data: [DONE]"
        text_content = ""
        for c in chunks:
            if c.startswith("data: ") and c.strip() != "data: [DONE]":
                data = json.loads(c[6:])
                delta = data.get("choices", [{}])[0].get("delta", {})
                text_content += delta.get("content", "")
        assert "Hello" in text_content

    @pytest.mark.asyncio
    async def test_multiple_text_blocks(self):
        """Multiple text content_blocks in a single streaming response should concatenate."""
        adapter = _make_vertex_adapter()

        events = [
            json.dumps(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6-20250514",
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                }
            ),
            # First text block
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Part one. "},
                }
            ),
            json.dumps({"type": "content_block_stop", "index": 0}),
            # Second text block
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "Part two."},
                }
            ),
            json.dumps({"type": "content_block_stop", "index": 1}),
            json.dumps(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 10},
                }
            ),
            json.dumps({"type": "message_stop"}),
        ]

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        # Both parts should appear
        text_content = ""
        for c in chunks:
            if c.startswith("data: ") and c.strip() != "data: [DONE]":
                data = json.loads(c[6:])
                delta = data.get("choices", [{}])[0].get("delta", {})
                text_content += delta.get("content", "")
        assert "Part one." in text_content
        assert "Part two." in text_content

    @pytest.mark.asyncio
    async def test_blank_lines_skipped(self):
        """Empty/whitespace-only lines from stream_post should be skipped."""
        adapter = _make_vertex_adapter()

        events = [
            "",
            "   ",
            json.dumps(
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6-20250514",
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                }
            ),
            "",
            json.dumps(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "OK"},
                }
            ),
            json.dumps({"type": "content_block_stop", "index": 0}),
            json.dumps(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                }
            ),
            json.dumps({"type": "message_stop"}),
        ]

        async def mock_stream(*args, **kwargs):
            for line in events:
                yield line

        adapter.http.stream_post = mock_stream

        chunks = []
        async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        assert chunks[-1].strip() == "data: [DONE]"
        text_content = ""
        for c in chunks:
            if c.startswith("data: ") and c.strip() != "data: [DONE]":
                data = json.loads(c[6:])
                delta = data.get("choices", [{}])[0].get("delta", {})
                text_content += delta.get("content", "")
        assert "OK" in text_content
