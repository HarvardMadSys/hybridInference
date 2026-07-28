"""Processor golden-file regression tests.

Covers every branch of GLMProcessor, QwenCoderProcessor, ThinkBlockProcessor,
DefaultProcessor, and the _clone_chunk helper as specified in
docs/developer/developer/processor-golden-tests.md.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from serving.adapters.processors import (
    DefaultProcessor,
    GLMProcessor,
    QwenCoderProcessor,
    ReasoningExtractProcessor,
    ThinkBlockProcessor,
    _clone_chunk,
    _pending_tag_len,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream_chunk(
    content: str | None = "",
    *,
    model: str = "test-model",
    finish_reason: str | None = None,
    tool_calls: list | None = None,
) -> dict[str, Any]:
    """Build a minimal streaming chunk for processor input."""
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _feed_stream(
    processor,
    chunks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Feed chunks through a processor, return (emitted, flushed)."""
    emitted: list[dict[str, Any]] = []
    for chunk in chunks:
        emitted.extend(processor.process_stream_chunk(chunk))
    flushed = processor.flush()
    return emitted, flushed


def _concat_content(chunks: list[dict[str, Any]]) -> str:
    """Concatenate all delta.content from chunks."""
    return "".join(
        c["choices"][0]["delta"].get("content", "") or "" for c in chunks if c.get("choices")
    )


def _concat_reasoning(chunks: list[dict[str, Any]]) -> str:
    """Concatenate all delta.reasoning_content from chunks."""
    return "".join(
        c["choices"][0]["delta"].get("reasoning_content", "") or ""
        for c in chunks
        if c.get("choices")
    )


def _get_tool_calls(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract tool_calls from first chunk that has them."""
    for c in chunks:
        if c.get("choices") and c["choices"][0].get("delta", {}).get("tool_calls"):
            return c["choices"][0]["delta"]["tool_calls"]
    return []


# ---------------------------------------------------------------------------
# GLMProcessor Tests (G-01 .. G-17)
# ---------------------------------------------------------------------------


class TestGLMProcessor:
    """Tests for GLMProcessor — XML tool call conversion and think-tag stripping."""

    def test_g01_plain_text_no_tags(self):
        """G-01: Plain text with no XML tags is emitted via stream + flush."""
        proc = GLMProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("Hello world", model="glm-4"),
            ],
        )
        all_chunks = emitted + flushed
        text = _concat_content(all_chunks)
        assert text == "Hello world"

    def test_g02_think_tags_stripped_content_kept(self):
        """G-02: <think> tags removed, thinking text emitted as content."""
        proc = GLMProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<think>reasoning</think>Hello", model="glm-4"),
            ],
        )
        all_chunks = emitted + flushed
        text = _concat_content(all_chunks)
        assert "Hello" in text
        assert "<think>" not in text
        assert "</think>" not in text
        assert "reasoning" in text  # GLM keeps think content, strips tags only

    def test_g03_single_tool_call(self):
        """G-03: Single tool call via XML is buffered, then flushed as tool_calls."""
        proc = GLMProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<tool_call>get_weather\n", model="glm-4"),
                _make_stream_chunk("<arg_key>city</arg_key>"),
                _make_stream_chunk("<arg_value>Beijing</arg_value></tool_call>"),
            ],
        )
        assert emitted == []  # all buffered in tool mode
        assert len(flushed) == 1
        tc = _get_tool_calls(flushed)
        assert len(tc) == 1
        assert tc[0]["function"]["name"] == "get_weather"
        assert json.loads(tc[0]["function"]["arguments"]) == {"city": "Beijing"}
        assert flushed[0]["choices"][0]["finish_reason"] == "tool_calls"

    def test_g04_tool_call_json_array_arg(self):
        """G-04: arg_value containing JSON array is parsed via json.loads."""
        proc = GLMProcessor()
        _emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "<tool_call>run_cmd\n<arg_key>cmd</arg_key>"
                    '<arg_value>["ls", "-la"]</arg_value></tool_call>',
                    model="glm-4",
                ),
            ],
        )
        tc = _get_tool_calls(flushed)
        args = json.loads(tc[0]["function"]["arguments"])
        assert args["cmd"] == ["ls", "-la"]

    def test_g05_tool_call_json_object_arg(self):
        """G-05: arg_value containing JSON object is parsed via json.loads."""
        proc = GLMProcessor()
        _emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "<tool_call>configure\n<arg_key>opts</arg_key>"
                    '<arg_value>{"key": "val"}</arg_value></tool_call>',
                    model="glm-4",
                ),
            ],
        )
        tc = _get_tool_calls(flushed)
        args = json.loads(tc[0]["function"]["arguments"])
        assert args["opts"] == {"key": "val"}

    def test_g06_malformed_xml_fallback(self):
        """G-06: Malformed tool XML (no name after tag) — raw buffer emitted as content fallback."""
        proc = GLMProcessor()
        # Use XML where _parse_glm_tool_xml returns None: no text after <tool_call>
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<tool_call>\n</tool_call>", model="glm-4"),
            ],
        )
        assert emitted == []
        assert len(flushed) == 1
        text = _concat_content(flushed)
        assert "<tool_call>" in text
        assert flushed[0]["choices"][0]["finish_reason"] is None

    def test_g07_content_none_passthrough(self):
        """G-07: Content None (keepalive) — chunk returned unchanged."""
        proc = GLMProcessor()
        chunk = _make_stream_chunk(content=None, model="glm-4")
        # Need to set content to None explicitly in delta
        chunk["choices"][0]["delta"]["content"] = None
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]

    def test_g08_partial_tag_at_boundary(self):
        """G-08: Partial '<' near end of buffer is held back, not emitted prematurely."""
        proc = GLMProcessor()
        # Send text ending with a partial '<'
        emitted1 = proc.process_stream_chunk(_make_stream_chunk("Hello world <", model="glm-4"))
        # The '<' near the end should be held back
        text1 = _concat_content(emitted1)
        assert "<" not in text1
        assert "Hello world " in text1

        # Next chunk completes — it's just text, not a tag
        emitted2 = proc.process_stream_chunk(_make_stream_chunk("not a tag>", model="glm-4"))
        flushed = proc.flush()
        all_after = emitted2 + flushed
        text_after = _concat_content(all_after)
        assert "<not a tag>" in text_after

    def test_g09a_think_emitted_before_tool_call_in_later_chunk(self):
        """G-09a: Think text fully emitted before <tool_call> arrives in a later chunk."""
        proc = GLMProcessor()
        # First chunk: complete think block + some text
        emitted1 = proc.process_stream_chunk(
            _make_stream_chunk("<think>reasoning</think>Preamble text here. ", model="glm-4")
        )
        # Text should be emitted (think tags stripped)
        text1 = _concat_content(emitted1)
        assert "<think>" not in text1

        # Second chunk: tool call
        emitted2 = proc.process_stream_chunk(
            _make_stream_chunk(
                "<tool_call>func\n<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>"
            )
        )
        flushed = proc.flush()

        # Tool mode should have been entered
        assert emitted2 == []
        tc = _get_tool_calls(flushed)
        assert tc[0]["function"]["name"] == "func"

    def test_g09b_think_and_tool_in_same_buffer(self):
        """G-09b: Think + tool_call in same buffer — think text emitted, tool call parsed."""
        proc = GLMProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "<think>thought</think><tool_call>func\n"
                    "<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>",
                    model="glm-4",
                ),
            ],
        )
        # Think text must be emitted (tags stripped, content kept — GLM contract)
        pre_text = _concat_content(emitted)
        assert "thought" in pre_text
        assert "<think>" not in pre_text
        # Tool call must still parse correctly
        tc = _get_tool_calls(flushed)
        assert tc[0]["function"]["name"] == "func"

    def test_g10_tool_tag_is_regular_text(self):
        """G-10: <tool> (not <tool_call>) is treated as regular text, not entering tool mode."""
        proc = GLMProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<tool>some_func</tool>", model="glm-4"),
            ],
        )
        text = _concat_content(emitted + flushed)
        assert "<tool>some_func</tool>" in text

    def test_g11_nonstream_think_strip(self):
        """G-11: Non-streaming — <think>/</ think> tags removed, content kept (GLM keeps think text)."""
        proc = GLMProcessor()
        response = {
            "choices": [
                {"message": {"content": "<think>internal</think>Hello"}, "finish_reason": "stop"}
            ],
        }
        result = proc.process_response(response)
        content = result["choices"][0]["message"]["content"]
        # GLM strips tags but keeps the think text itself
        assert "<think>" not in content
        assert "</think>" not in content
        assert "internal" in content
        assert "Hello" in content

    def test_g12_nonstream_empty_content(self):
        """G-12: Non-streaming — empty content returned unchanged."""
        proc = GLMProcessor()
        response = {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        }
        result = proc.process_response(response)
        assert result["choices"][0]["message"]["content"] == ""

    def test_g13_nonstream_no_choices(self):
        """G-13: Non-streaming — no choices returns response unchanged."""
        proc = GLMProcessor()
        response = {"choices": []}
        result = proc.process_response(response)
        assert result == {"choices": []}

    def test_g14_stream_no_choices(self):
        """G-14: Stream chunk with no choices — returned unchanged (passthrough)."""
        proc = GLMProcessor()
        chunk = {"choices": [], "model": "glm-4"}
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]

    def test_g15_flush_empty_buffer(self):
        """G-15: Flush with empty buffer returns []."""
        proc = GLMProcessor()
        assert proc.flush() == []

    def test_g16_flush_non_tool_remaining_text(self):
        """G-16: Text with partial tag — total output preserves all content after flush."""
        proc = GLMProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("leftover<", model="glm-4"),
            ],
        )
        text = _concat_content(emitted + flushed)
        assert text == "leftover<"

    def test_g17_json_loads_failure_raw_string(self):
        """G-17: json.loads failure in arg value — value kept as raw string."""
        proc = GLMProcessor()
        _emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "<tool_call>func\n<arg_key>data</arg_key>"
                    "<arg_value>[not valid json</arg_value></tool_call>",
                    model="glm-4",
                ),
            ],
        )
        tc = _get_tool_calls(flushed)
        args = json.loads(tc[0]["function"]["arguments"])
        assert args["data"] == "[not valid json"

    @pytest.mark.parametrize(
        "chunk,description",
        [
            ({"choices": [], "model": "glm-4"}, "G-14: no choices"),
            (
                {"choices": [{"index": 0, "delta": {"content": None}}], "model": "glm-4"},
                "G-07: content None",
            ),
        ],
        ids=lambda x: x if isinstance(x, str) else "",
    )
    def test_glm_passthrough(self, chunk, description):
        """GLM passthrough guard cases — no choices or content None."""
        proc = GLMProcessor()
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]


# ---------------------------------------------------------------------------
# QwenCoderProcessor Tests (Q-01 .. Q-22)
# ---------------------------------------------------------------------------


class TestQwenCoderProcessor:
    """Tests for QwenCoderProcessor — Qwen3-Coder XML tool call conversion."""

    def test_q01_plain_text(self):
        """Q-01: Plain text with no tool calls is emitted via stream + flush."""
        proc = QwenCoderProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("Hello world", model="qwen3-coder"),
            ],
        )
        all_chunks = emitted + flushed
        text = _concat_content(all_chunks)
        assert text == "Hello world"

    def test_q02_single_tool_call(self):
        """Q-02: Single tool call — pre-text emitted, tool XML buffered, flush emits tool_calls."""
        proc = QwenCoderProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "I'll search for that.\n<tool_call>\n<function=search>\n"
                    "<parameter=query>hello</parameter>\n</function>\n</tool_call>",
                    model="qwen3-coder",
                ),
            ],
        )
        # Pre-text should have been emitted
        pre_text = _concat_content(emitted)
        assert "search for that" in pre_text
        # Flush should have tool calls
        tc = _get_tool_calls(flushed)
        assert tc[0]["function"]["name"] == "search"
        args = json.loads(tc[0]["function"]["arguments"])
        assert args["query"] == "hello"
        assert flushed[0]["choices"][0]["finish_reason"] == "tool_calls"

    def test_q03_parallel_tool_calls(self):
        """Q-03: Two tool_call blocks parsed with correct indices."""
        proc = QwenCoderProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "<tool_call>\n<function=read_file>\n<parameter=path>/etc/hosts</parameter>\n"
                    "</function>\n</tool_call>\n<tool_call>\n<function=search>\n"
                    "<parameter=query>hello</parameter>\n</function>\n</tool_call>",
                    model="qwen3-coder",
                ),
            ],
        )
        assert emitted == []
        tc = _get_tool_calls(flushed)
        assert len(tc) == 2
        assert tc[0]["function"]["name"] == "read_file"
        assert tc[0]["index"] == 0
        assert tc[1]["function"]["name"] == "search"
        assert tc[1]["index"] == 1

    def test_q04_native_tool_calls_passthrough(self):
        """Q-04: Native tool_calls in delta passed through untouched."""
        proc = QwenCoderProcessor()
        chunk = _make_stream_chunk(
            content=None,
            tool_calls=[{"id": "call_1", "function": {"name": "test"}}],
            model="qwen3-coder",
        )
        # Ensure content key is absent so delta only has tool_calls
        chunk["choices"][0]["delta"].pop("content", None)
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]

    def test_q05_content_none(self):
        """Q-05: Content is None (non-string) — returns []."""
        proc = QwenCoderProcessor()
        chunk = _make_stream_chunk(model="qwen3-coder")
        chunk["choices"][0]["delta"]["content"] = None
        result = proc.process_stream_chunk(chunk)
        assert result == []

    def test_q06_unclosed_tool_call_fallback(self):
        """Q-06: Unclosed <tool_call> — fallback parsing via <function= match."""
        proc = QwenCoderProcessor()
        _emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "<tool_call>\n<function=search>\n<parameter=q>test</parameter>\n</function>",
                    model="qwen3-coder",
                ),
            ],
        )
        # No closing </tool_call>, but fallback should still parse
        tc = _get_tool_calls(flushed)
        assert tc[0]["function"]["name"] == "search"

    def test_q07_param_newline_stripping(self):
        """Q-07: Leading/trailing newlines stripped from parameter values."""
        proc = QwenCoderProcessor()
        _emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "<tool_call>\n<function=write>\n<parameter=content>\nline1\nline2\n</parameter>\n"
                    "</function>\n</tool_call>",
                    model="qwen3-coder",
                ),
            ],
        )
        tc = _get_tool_calls(flushed)
        args = json.loads(tc[0]["function"]["arguments"])
        # Leading and trailing \n stripped, internal preserved
        assert args["content"] == "line1\nline2"

    def test_q08_param_json_object(self):
        """Q-08: Parameter with JSON object value parsed via json.loads."""
        proc = QwenCoderProcessor()
        _emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    '<tool_call>\n<function=configure>\n<parameter=opts>{"key": "val"}</parameter>\n'
                    "</function>\n</tool_call>",
                    model="qwen3-coder",
                ),
            ],
        )
        tc = _get_tool_calls(flushed)
        args = json.loads(tc[0]["function"]["arguments"])
        assert args["opts"] == {"key": "val"}

    def test_q09_param_json_array(self):
        """Q-09: Parameter with JSON array value parsed via json.loads."""
        proc = QwenCoderProcessor()
        _emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    '<tool_call>\n<function=run>\n<parameter=cmd>["ls", "-la"]</parameter>\n'
                    "</function>\n</tool_call>",
                    model="qwen3-coder",
                ),
            ],
        )
        tc = _get_tool_calls(flushed)
        args = json.loads(tc[0]["function"]["arguments"])
        assert args["cmd"] == ["ls", "-la"]

    def test_q10_param_json_parse_failure(self):
        """Q-10: JSON parse failure — value kept as raw string."""
        proc = QwenCoderProcessor()
        _emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "<tool_call>\n<function=run>\n<parameter=data>[broken json</parameter>\n"
                    "</function>\n</tool_call>",
                    model="qwen3-coder",
                ),
            ],
        )
        tc = _get_tool_calls(flushed)
        args = json.loads(tc[0]["function"]["arguments"])
        assert args["data"] == "[broken json"

    def test_q11_nonstream_tool_call_conversion(self):
        """Q-11: Non-streaming — tool call XML converted, pre-text kept as content."""
        proc = QwenCoderProcessor()
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            "Here you go.\n<tool_call>\n<function=search>\n"
                            "<parameter=query>hello</parameter>\n</function>\n</tool_call>"
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        result = proc.process_response(response)
        msg = result["choices"][0]["message"]
        assert msg["content"] == "Here you go."
        assert msg["tool_calls"][0]["function"]["name"] == "search"
        assert result["choices"][0]["finish_reason"] == "tool_calls"

    def test_q12_nonstream_no_tool_calls(self):
        """Q-12: Non-streaming — no tool calls, returned unchanged."""
        proc = QwenCoderProcessor()
        response = {
            "choices": [{"message": {"content": "Just text"}, "finish_reason": "stop"}],
        }
        result = proc.process_response(response)
        assert result["choices"][0]["message"]["content"] == "Just text"

    def test_q13_nonstream_empty_content(self):
        """Q-13: Non-streaming — empty content returned unchanged."""
        proc = QwenCoderProcessor()
        response = {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        }
        result = proc.process_response(response)
        assert result["choices"][0]["message"]["content"] == ""

    def test_q14_pre_tool_text_emitted(self):
        """Q-14: Pre-tool text emitted as chunk before entering tool mode."""
        proc = QwenCoderProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "Let me search.\n<tool_call>\n<function=search>\n"
                    "<parameter=q>test</parameter>\n</function>\n</tool_call>",
                    model="qwen3-coder",
                ),
            ],
        )
        pre_text = _concat_content(emitted)
        assert "Let me search." in pre_text
        tc = _get_tool_calls(flushed)
        assert tc[0]["function"]["name"] == "search"

    def test_q15_pre_tool_whitespace_only(self):
        """Q-15: Pre-tool text is whitespace only — no pre-text chunk emitted."""
        proc = QwenCoderProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "  \n<tool_call>\n<function=search>\n"
                    "<parameter=q>test</parameter>\n</function>\n</tool_call>",
                    model="qwen3-coder",
                ),
            ],
        )
        # Whitespace-only pre-text stripped to empty — no content chunk emitted
        assert emitted == []
        tc = _get_tool_calls(flushed)
        assert tc[0]["function"]["name"] == "search"

    def test_q16_no_choices(self):
        """Q-16: Stream chunk with no choices — returned unchanged (passthrough)."""
        proc = QwenCoderProcessor()
        chunk = {"choices": [], "model": "qwen3-coder"}
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]

    def test_q17_flush_empty_buffer(self):
        """Q-17: Flush with empty buffer returns []."""
        proc = QwenCoderProcessor()
        assert proc.flush() == []

    def test_q18_flush_non_tool_remaining_text(self):
        """Q-18: Text with partial tag — total output preserves all content after flush."""
        proc = QwenCoderProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("leftover<", model="qwen3-coder"),
            ],
        )
        text = _concat_content(emitted + flushed)
        assert "leftover" in text
        assert "<" in text

    def test_q19_flush_tool_parse_failure(self):
        """Q-19: Flush tool parse failure — raw buffer emitted as content."""
        proc = QwenCoderProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "<tool_call>garbage with no function tag</tool_call>",
                    model="qwen3-coder",
                ),
            ],
        )
        assert emitted == []
        # Parse fails — no <function= found after fallback
        assert len(flushed) == 1
        text = _concat_content(flushed)
        assert "garbage" in text
        assert flushed[0]["choices"][0]["finish_reason"] is None

    def test_q20_nonstream_tool_at_start_no_pretext(self):
        """Q-20: Non-streaming — tool_call at start, content set to None."""
        proc = QwenCoderProcessor()
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            "<tool_call>\n<function=search>\n"
                            "<parameter=q>test</parameter>\n</function>\n</tool_call>"
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        result = proc.process_response(response)
        msg = result["choices"][0]["message"]
        assert msg["content"] is None
        assert msg["tool_calls"][0]["function"]["name"] == "search"

    def test_q21_no_function_match_in_block(self):
        """Q-21: _FUNCTION_RE no match inside block — returns None."""
        proc = QwenCoderProcessor()
        # Manually test the parser with a block that has no <function= tag
        result = proc._parse_qwen_tool_xml("<tool_call>no function here</tool_call>")
        assert result is None

    def test_q22_nonstream_no_choices(self):
        """Q-22: Non-streaming — no choices returns response unchanged."""
        proc = QwenCoderProcessor()
        response = {"choices": []}
        result = proc.process_response(response)
        assert result == {"choices": []}


# ---------------------------------------------------------------------------
# ThinkBlockProcessor Tests (T-01 .. T-17)
# ---------------------------------------------------------------------------


class TestThinkBlockProcessor:
    """Tests for ThinkBlockProcessor — strips entire <think> blocks."""

    def test_t01_no_think_blocks(self):
        """T-01: No think blocks — text emitted normally."""
        proc = ThinkBlockProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("Hello world", model="minimax-m1"),
            ],
        )
        all_chunks = emitted + flushed
        text = _concat_content(all_chunks)
        assert text == "Hello world"

    def test_t02_complete_think_block_one_chunk(self):
        """T-02: Complete <think>...</think> in one chunk — entirely discarded."""
        proc = ThinkBlockProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<think>internal reasoning</think>", model="minimax-m1"),
            ],
        )
        all_chunks = emitted + flushed
        text = _concat_content(all_chunks)
        assert text == ""
        assert "internal" not in text

    def test_t03_think_block_split_across_chunks(self):
        """T-03: Think block split across chunks — content discarded across chunks."""
        proc = ThinkBlockProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<think>start of ", model="minimax-m1"),
                _make_stream_chunk("reasoning</think>"),
            ],
        )
        all_chunks = emitted + flushed
        text = _concat_content(all_chunks)
        assert "reasoning" not in text
        assert "<think>" not in text

    def test_t04_text_before_and_after_think(self):
        """T-04: Text before and after think block — pre/post text emitted, think discarded."""
        proc = ThinkBlockProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "Hello <think>internal reasoning</think> World", model="minimax-m1"
                ),
            ],
        )
        all_chunks = emitted + flushed
        text = _concat_content(all_chunks)
        assert "Hello" in text
        assert "World" in text
        assert "internal" not in text

    def test_t05_multiple_think_blocks(self):
        """T-05: Multiple think blocks interleaved with text — all blocks stripped."""
        proc = ThinkBlockProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "A<think>thought1</think>B<think>thought2</think>C",
                    model="minimax-m1",
                ),
            ],
        )
        all_chunks = emitted + flushed
        text = _concat_content(all_chunks)
        assert "A" in text
        assert "B" in text
        assert "C" in text
        assert "thought1" not in text
        assert "thought2" not in text

    def test_t06_partial_close_tag_at_boundary(self):
        """T-06: Partial </think> at chunk boundary — 8-char tail retained, resolved next chunk."""
        proc = ThinkBlockProcessor()
        # Start think block
        emitted1 = proc.process_stream_chunk(
            _make_stream_chunk("<think>reasoning content here</thi", model="minimax-m1")
        )
        assert _concat_content(emitted1) == ""  # all in think mode, nothing emitted

        # Complete the closing tag
        emitted2 = proc.process_stream_chunk(
            _make_stream_chunk("nk>After think", model="minimax-m1")
        )
        flushed = proc.flush()
        all_after = emitted2 + flushed
        text = _concat_content(all_after)
        assert "After think" in text
        assert "reasoning" not in text

    def test_t07_incomplete_think_at_end_of_stream(self):
        """T-07: Incomplete think block at end of stream — discarded by flush."""
        proc = ThinkBlockProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<think>never closed reasoning", model="minimax-m1"),
            ],
        )
        # Flush with in_think=True returns []
        assert flushed == []
        # Stream also shouldn't have emitted think content
        text = _concat_content(emitted)
        assert "never closed" not in text

    def test_t08_native_tool_calls_passthrough(self):
        """T-08: Native tool_calls in delta passed through untouched."""
        proc = ThinkBlockProcessor()
        chunk = _make_stream_chunk(
            content=None,
            tool_calls=[{"id": "call_1", "function": {"name": "test"}}],
            model="minimax-m1",
        )
        chunk["choices"][0]["delta"].pop("content", None)
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]
        assert result[0] is chunk

    def test_t09_content_none(self):
        """T-09: Content is None (non-string) — returns []."""
        proc = ThinkBlockProcessor()
        chunk = _make_stream_chunk(model="minimax-m1")
        chunk["choices"][0]["delta"]["content"] = None
        result = proc.process_stream_chunk(chunk)
        assert result == []

    def test_t10_nonstream_think_strip(self):
        """T-10: Non-streaming — regex removes all <think> blocks, result stripped."""
        proc = ThinkBlockProcessor()
        response = {
            "choices": [
                {
                    "message": {"content": "<think>internal</think>Hello <think>more</think>World"},
                    "finish_reason": "stop",
                }
            ],
        }
        result = proc.process_response(response)
        content = result["choices"][0]["message"]["content"]
        assert "Hello" in content
        assert "World" in content
        assert "<think>" not in content
        assert "internal" not in content

    def test_t11_nonstream_empty_content(self):
        """T-11: Non-streaming — empty content returned unchanged."""
        proc = ThinkBlockProcessor()
        response = {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        }
        result = proc.process_response(response)
        assert result["choices"][0]["message"]["content"] == ""

    def test_t12_flush_remaining_text_no_think(self):
        """T-12: Text with partial tag — total output preserves all content after flush."""
        proc = ThinkBlockProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("leftover<", model="minimax-m1"),
            ],
        )
        text = _concat_content(emitted + flushed)
        assert "leftover" in text
        assert "<" in text

    def test_t13_flush_empty_buffer(self):
        """T-13: Flush with empty buffer returns []."""
        proc = ThinkBlockProcessor()
        assert proc.flush() == []

    def test_t14_no_choices(self):
        """T-14: Stream chunk with no choices — returned unchanged (passthrough)."""
        proc = ThinkBlockProcessor()
        chunk = {"choices": [], "model": "minimax-m1"}
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]
        assert result[0] is chunk

    def test_t15_nonstream_no_choices(self):
        """T-15: Non-streaming — no choices returns response unchanged."""
        proc = ThinkBlockProcessor()
        response = {"choices": []}
        result = proc.process_response(response)
        assert result == {"choices": []}

    def test_t16_partial_tag_near_end(self):
        """T-16: Partial '<' near end of buffer (no think) — safe-index holds back partial tag."""
        proc = ThinkBlockProcessor()
        emitted = proc.process_stream_chunk(_make_stream_chunk("text<", model="minimax-m1"))
        text = _concat_content(emitted)
        assert "text" in text
        assert "<" not in text
        # The '<' is still in buffer
        flushed = proc.flush()
        flush_text = _concat_content(flushed)
        assert "<" in flush_text

    def test_t17_flush_complete_think_in_buffer(self):
        """T-17: Flush with complete think block in buffer — regex removes it."""
        proc = ThinkBlockProcessor()
        # Manually set buffer with a complete think block + text
        proc.buffer = "Before<think>thought</think>After"
        proc.in_think = False
        flushed = proc.flush()
        text = _concat_content(flushed)
        assert "Before" in text
        assert "After" in text
        assert "thought" not in text

    @pytest.mark.parametrize(
        "chunk,expected_len",
        [
            (
                _make_stream_chunk(
                    content=None,
                    tool_calls=[{"id": "c1", "function": {"name": "f"}}],
                    model="mm",
                ),
                1,
            ),
            ({"choices": [], "model": "mm"}, 1),
        ],
        ids=["T-08-native-tool-calls", "T-14-no-choices"],
    )
    def test_think_block_passthrough(self, chunk, expected_len):
        """ThinkBlockProcessor passthrough guard — native tool_calls or no choices."""
        proc = ThinkBlockProcessor()
        result = proc.process_stream_chunk(chunk)
        assert len(result) == expected_len
        assert result[0] is chunk

    def test_usage_chunk_with_empty_content_preserved(self):
        """Regression: MiniMax sends a final usage chunk with content="" and usage data.

        ThinkBlockProcessor must pass it through so the adapter captures
        prompt_tokens_details.cached_tokens and reasoning_tokens.
        See: MiniMax streaming sends a 4th chunk with usage + empty delta.
        """
        proc = ThinkBlockProcessor()
        chunk = {
            "id": "test",
            "choices": [
                {"finish_reason": "stop", "index": 0, "delta": {"content": "", "role": "assistant"}}
            ],
            "usage": {
                "prompt_tokens": 111,
                "completion_tokens": 47,
                "total_tokens": 158,
                "completion_tokens_details": {"reasoning_tokens": 42},
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        }
        result = proc.process_stream_chunk(chunk)
        assert len(result) == 1
        assert result[0] is chunk
        assert result[0]["usage"]["prompt_tokens_details"]["cached_tokens"] == 80

    def test_usage_chunk_with_none_content_preserved(self):
        """Regression: chunk with usage and no content key in delta must be preserved."""
        proc = ThinkBlockProcessor()
        chunk = {
            "id": "test",
            "choices": [{"finish_reason": "stop", "index": 0, "delta": {}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }
        result = proc.process_stream_chunk(chunk)
        assert len(result) == 1
        assert result[0] is chunk

    @pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "thinking"])
    def test_native_reasoning_fields_preserved(self, field: str):
        """Native reasoning fields are structured deltas, not literal <think> content."""
        proc = ThinkBlockProcessor()
        chunk = {
            "id": "test",
            "choices": [{"finish_reason": None, "index": 0, "delta": {field: "thinking..."}}],
        }
        result = proc.process_stream_chunk(chunk)
        assert len(result) == 1
        assert result[0] is chunk
        assert result[0]["choices"][0]["delta"][field] == "thinking..."

    def test_empty_chunk_with_finish_reason_forwards_signal(self):
        """Empty-content chunk carrying finish_reason forwards a signal-only chunk.

        The finish_reason must reach the adapter's bookkeeping (a swallowed
        "length" would otherwise be misreported as "stop"); the emptied delta
        produces no client-visible output.
        """
        proc = ThinkBlockProcessor()
        chunk = {
            "id": "test",
            "choices": [{"finish_reason": "length", "index": 0, "delta": {"content": ""}}],
        }
        result = proc.process_stream_chunk(chunk)
        assert len(result) == 1
        assert result[0]["choices"][0]["finish_reason"] == "length"
        assert result[0]["choices"][0]["delta"] == {}

    def test_empty_chunk_without_signals_dropped(self):
        """Chunk with empty content, no usage, and no finish_reason is dropped."""
        proc = ThinkBlockProcessor()
        chunk = {
            "id": "test",
            "choices": [{"finish_reason": None, "index": 0, "delta": {"content": ""}}],
        }
        result = proc.process_stream_chunk(chunk)
        assert result == []


# ---------------------------------------------------------------------------
# DefaultProcessor Tests (D-01 .. D-03)
# ---------------------------------------------------------------------------


class TestDefaultProcessor:
    """Tests for DefaultProcessor — pure pass-through."""

    def test_d01_stream_passthrough(self):
        """D-01: Stream chunk passthrough — returns [chunk], same object."""
        proc = DefaultProcessor()
        chunk = _make_stream_chunk("Hello", model="gpt-4")
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]
        assert result[0] is chunk

    def test_d02_response_passthrough(self):
        """D-02: Response passthrough — returns response unchanged, same object."""
        proc = DefaultProcessor()
        response = {
            "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
        }
        result = proc.process_response(response)
        assert result is response

    def test_d03_flush_empty(self):
        """D-03: Flush returns [] — no buffered content."""
        proc = DefaultProcessor()
        assert proc.flush() == []


# ---------------------------------------------------------------------------
# _clone_chunk Tests (C-01 .. C-04)
# ---------------------------------------------------------------------------


class TestCloneChunk:
    """Tests for _clone_chunk helper — deep copy of chunk structure."""

    def test_c01_mutation_isolation(self):
        """C-01: Modifying a cloned chunk must not affect the original."""
        original = {
            "id": "test",
            "choices": [{"index": 0, "delta": {"content": "hello"}}],
        }
        cloned = _clone_chunk(original)
        cloned["choices"][0]["delta"]["content"] = "CHANGED"

        assert original["choices"][0]["delta"]["content"] == "hello"

    def test_c02_no_choices_key(self):
        """C-02: Chunk without 'choices' key — only top-level copy, no crash."""
        original = {"id": "test", "model": "gpt-4"}
        cloned = _clone_chunk(original)
        cloned["id"] = "CHANGED"

        assert original["id"] == "test"

    def test_c03_multiple_choices(self):
        """C-03: Multiple choices — each choice and its delta independently copied."""
        original = {
            "id": "test",
            "choices": [
                {"index": 0, "delta": {"content": "a"}},
                {"index": 1, "delta": {"content": "b"}},
            ],
        }
        cloned = _clone_chunk(original)
        cloned["choices"][0]["delta"]["content"] = "X"
        cloned["choices"][1]["delta"]["content"] = "Y"

        assert original["choices"][0]["delta"]["content"] == "a"
        assert original["choices"][1]["delta"]["content"] == "b"

    def test_c04_choice_without_delta(self):
        """C-04: Choice without 'delta' key — choice is copied, no crash."""
        original = {
            "id": "test",
            "choices": [{"index": 0, "finish_reason": "stop"}],
        }
        cloned = _clone_chunk(original)
        cloned["choices"][0]["finish_reason"] = "CHANGED"

        assert original["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# Stream-signal preservation and parallel tool calls (S-01 .. S-07)
# ---------------------------------------------------------------------------


class TestStreamSignalPreservation:
    """Buffering processors must not swallow finish_reason/usage signals."""

    def test_s01_glm_finish_reason_forwarded_while_buffering(self):
        """GLM in tool mode forwards a swallowed finish_reason chunk."""
        proc = GLMProcessor()
        assert proc.process_stream_chunk(_make_stream_chunk("<tool_call>get_weather\n")) == []
        out = proc.process_stream_chunk(_make_stream_chunk(None, finish_reason="length"))
        assert len(out) == 1
        assert out[0]["choices"][0]["finish_reason"] == "length"
        assert out[0]["choices"][0]["delta"] == {}

    def test_s02_glm_flush_preserves_length_finish(self):
        """A tool call truncated at max_tokens must flush as "length"."""
        proc = GLMProcessor()
        proc.process_stream_chunk(
            _make_stream_chunk("<tool_call>get_weather\n<arg_key>city</arg_key>")
        )
        proc.process_stream_chunk(_make_stream_chunk(None, finish_reason="length"))
        flushed = proc.flush()
        assert flushed[0]["choices"][0]["finish_reason"] == "length"

    def test_s03_glm_flush_promotes_stop_to_tool_calls(self):
        """A complete tool call still overrides upstream "stop"."""
        proc = GLMProcessor()
        proc.process_stream_chunk(
            _make_stream_chunk(
                "<tool_call>get_weather\n<arg_key>city</arg_key>"
                "<arg_value>Beijing</arg_value></tool_call>"
            )
        )
        proc.process_stream_chunk(_make_stream_chunk(None, finish_reason="stop"))
        flushed = proc.flush()
        assert flushed[0]["choices"][0]["finish_reason"] == "tool_calls"

    def test_s04_qwen_finish_and_usage_forwarded_while_buffering(self):
        """Qwen in tool mode forwards finish_reason and choice-attached usage."""
        proc = QwenCoderProcessor()
        assert proc.process_stream_chunk(_make_stream_chunk("<tool_call>\n")) == []
        chunk = _make_stream_chunk(None, finish_reason="length")
        chunk["usage"] = {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
        out = proc.process_stream_chunk(chunk)
        assert len(out) == 1
        assert out[0]["choices"][0]["finish_reason"] == "length"
        assert out[0]["usage"]["total_tokens"] == 12
        assert out[0]["choices"][0]["delta"] == {}

    def test_s05_qwen_flush_preserves_length_finish(self):
        """A Qwen tool call truncated at max_tokens must flush as "length"."""
        proc = QwenCoderProcessor()
        proc.process_stream_chunk(
            _make_stream_chunk("<tool_call>\n<function=get_weather>\n<parameter=city>Paris")
        )
        proc.process_stream_chunk(_make_stream_chunk(None, finish_reason="length"))
        flushed = proc.flush()
        assert flushed[0]["choices"][0]["finish_reason"] == "length"

    def test_s06_glm_parallel_tool_calls_parsed_separately(self):
        """Two <tool_call> blocks produce two calls with per-block args."""
        proc = GLMProcessor()
        proc.process_stream_chunk(
            _make_stream_chunk(
                "<tool_call>get_weather\n"
                "<arg_key>city</arg_key><arg_value>Paris</arg_value></tool_call>"
                "<tool_call>get_time\n"
                "<arg_key>tz</arg_key><arg_value>UTC</arg_value></tool_call>"
            )
        )
        flushed = proc.flush()
        tool_calls = _get_tool_calls(flushed)
        assert len(tool_calls) == 2
        assert tool_calls[0]["function"]["name"] == "get_weather"
        assert json.loads(tool_calls[0]["function"]["arguments"]) == {"city": "Paris"}
        assert tool_calls[1]["function"]["name"] == "get_time"
        assert json.loads(tool_calls[1]["function"]["arguments"]) == {"tz": "UTC"}
        assert tool_calls[0]["index"] != tool_calls[1]["index"]
        assert tool_calls[0]["id"] != tool_calls[1]["id"]

    def test_s07_glm_no_signal_chunks_still_dropped(self):
        """Buffered chunks without finish_reason/usage stay swallowed."""
        proc = GLMProcessor()
        proc.process_stream_chunk(_make_stream_chunk("<tool_call>get_weather\n"))
        assert proc.process_stream_chunk(_make_stream_chunk("<arg_key>a</arg_key>")) == []


# ---------------------------------------------------------------------------
# ReasoningExtractProcessor Tests (R-01 .. R-20)
# ---------------------------------------------------------------------------


class TestReasoningExtractProcessor:
    """Tests for ReasoningExtractProcessor — lift <think>/<mm:think> into reasoning."""

    def test_r01_plain_text_no_tags(self):
        """R-01: No reasoning tags — text streams as content, reasoning stays empty."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc, [_make_stream_chunk("Hello world", model="minimax-m3")]
        )
        chunks = emitted + flushed
        assert _concat_content(chunks) == "Hello world"
        assert _concat_reasoning(chunks) == ""

    def test_r02_complete_mm_think_one_chunk(self):
        """R-02: <mm:think>...</mm:think> in one chunk — inner to reasoning, rest to content."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc, [_make_stream_chunk("<mm:think>reasoning</mm:think>Answer", model="minimax-m3")]
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "reasoning"
        assert _concat_content(chunks) == "Answer"

    def test_r03_mm_think_streamed_across_chunks(self):
        """R-03: MiniMax-M3 real shape — tags alone, reasoning and answer in separate chunks."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<mm:think>", model="minimax-m3"),
                _make_stream_chunk("step one "),
                _make_stream_chunk("step two"),
                _make_stream_chunk("</mm:think>"),
                _make_stream_chunk("Final answer."),
            ],
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "step one step two"
        assert _concat_content(chunks) == "Final answer."

    def test_r04_think_not_matched_by_default(self):
        """R-04: <think> is not a default tag; on an M3 route it streams as content."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc, [_make_stream_chunk("<think>thoughts</think>Reply", model="minimax-m3")]
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == ""
        assert _concat_content(chunks) == "<think>thoughts</think>Reply"

    def test_r04b_configurable_think_pair(self):
        """R-04b: a processor built with a <think> pair extracts <think> reasoning."""
        proc = ReasoningExtractProcessor(tag_pairs=(("<think>", "</think>"),))
        emitted, flushed = _feed_stream(
            proc, [_make_stream_chunk("<think>thoughts</think>Reply", model="some-model")]
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "thoughts"
        assert _concat_content(chunks) == "Reply"

    def test_r05_text_before_and_after(self):
        """R-05: content before and after a reasoning block both reach the content channel."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc, [_make_stream_chunk("pre <mm:think>mid</mm:think> post", model="minimax-m3")]
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "mid"
        assert _concat_content(chunks) == "pre  post"

    def test_r06_split_closing_tag(self):
        """R-06: A closing tag split across chunks is reassembled, no leak."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<mm:think>reasoning</mm:", model="minimax-m3"),
                _make_stream_chunk("think>answer"),
            ],
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "reasoning"
        assert _concat_content(chunks) == "answer"
        assert "</mm:" not in _concat_reasoning(chunks)
        assert "</mm:" not in _concat_content(chunks)

    def test_r07_split_opening_tag(self):
        """R-07: An opening tag split across chunks is reassembled."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<mm:", model="minimax-m3"),
                _make_stream_chunk("think>reasoning</mm:think>done"),
            ],
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "reasoning"
        assert _concat_content(chunks) == "done"

    def test_r08_native_tool_calls_passthrough(self):
        """R-08: Native tool_calls delta passed through untouched."""
        proc = ReasoningExtractProcessor()
        chunk = _make_stream_chunk(
            content=None,
            tool_calls=[{"id": "c1", "function": {"name": "f"}}],
            model="minimax-m3",
        )
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]
        assert result[0] is chunk

    def test_r09_content_none(self):
        """R-09: Content None (non-string) with no signals — returns []."""
        proc = ReasoningExtractProcessor()
        chunk = _make_stream_chunk(model="minimax-m3")
        chunk["choices"][0]["delta"]["content"] = None
        assert proc.process_stream_chunk(chunk) == []

    @pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "thinking"])
    def test_r10_structured_reasoning_passthrough(self, field):
        """R-10: Already-structured reasoning deltas pass through untouched."""
        proc = ReasoningExtractProcessor()
        chunk = {
            "id": "t",
            "choices": [{"index": 0, "delta": {field: "thinking..."}, "finish_reason": None}],
        }
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]
        assert result[0] is chunk

    def test_r11_usage_chunk_preserved(self):
        """R-11: A terminal usage-bearing chunk is passed through."""
        proc = ReasoningExtractProcessor()
        chunk = {
            "id": "t",
            "choices": [{"finish_reason": "stop", "index": 0, "delta": {"content": ""}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }
        result = proc.process_stream_chunk(chunk)
        assert len(result) == 1
        assert result[0] is chunk

    def test_r12_finish_reason_forwarded_while_buffering(self):
        """R-12: An empty-content chunk carrying finish_reason forwards a signal-only chunk."""
        proc = ReasoningExtractProcessor()
        chunk = {
            "id": "t",
            "choices": [{"finish_reason": "length", "index": 0, "delta": {"content": ""}}],
        }
        result = proc.process_stream_chunk(chunk)
        assert len(result) == 1
        assert result[0]["choices"][0]["finish_reason"] == "length"
        assert result[0]["choices"][0]["delta"] == {}

    def test_r13_truncated_reasoning_flushed_as_reasoning(self):
        """R-13: Reasoning cut off with no closing tag is surfaced as reasoning, not dropped."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc, [_make_stream_chunk("<mm:think>partial thought", model="minimax-m3")]
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "partial thought"
        assert _concat_content(chunks) == ""

    def test_r14_no_choices_passthrough(self):
        """R-14: Chunk with no choices returned unchanged."""
        proc = ReasoningExtractProcessor()
        chunk = {"choices": [], "model": "minimax-m3"}
        result = proc.process_stream_chunk(chunk)
        assert result == [chunk]
        assert result[0] is chunk

    def test_r15_reasoning_contains_angle_bracket(self):
        """R-15: '<' inside reasoning is preserved, not treated as a tag start."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [_make_stream_chunk("<mm:think>if a < b and c<d</mm:think>ok", model="minimax-m3")],
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "if a < b and c<d"
        assert _concat_content(chunks) == "ok"

    def test_r16_flush_partial_open_tag_as_content(self):
        """R-16: A held-back partial opening tag that never completes flushes as content."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(proc, [_make_stream_chunk("plain<mm:", model="minimax-m3")])
        chunks = emitted + flushed
        assert _concat_content(chunks) == "plain<mm:"
        assert _concat_reasoning(chunks) == ""

    def test_r17_nonstream_extracts_reasoning(self):
        """R-17: Non-streaming — tags lifted out of content into reasoning_content."""
        proc = ReasoningExtractProcessor()
        response = {
            "choices": [
                {
                    "message": {"content": "<mm:think>the reasoning</mm:think>The answer."},
                    "finish_reason": "stop",
                }
            ],
        }
        result = proc.process_response(response)
        msg = result["choices"][0]["message"]
        assert msg["content"] == "The answer."
        assert msg["reasoning_content"] == "the reasoning"

    def test_r18_nonstream_no_tags_unchanged(self):
        """R-18: Non-streaming — content without tags is left untouched."""
        proc = ReasoningExtractProcessor()
        response = {
            "choices": [{"message": {"content": "just an answer"}, "finish_reason": "stop"}],
        }
        result = proc.process_response(response)
        msg = result["choices"][0]["message"]
        assert msg["content"] == "just an answer"
        assert "reasoning_content" not in msg

    def test_r18b_nonstream_unterminated_reasoning(self):
        """R-18b: Non-streaming — an unterminated <mm:think> (truncated) becomes reasoning."""
        proc = ReasoningExtractProcessor()
        response = {
            "choices": [
                {
                    "message": {"content": "<mm:think>partial thought with no close"},
                    "finish_reason": "length",
                }
            ],
        }
        result = proc.process_response(response)
        msg = result["choices"][0]["message"]
        assert msg["content"] == ""
        assert msg["reasoning_content"] == "partial thought with no close"

    def test_r19_nonstream_appends_to_existing_reasoning(self):
        """R-19: Non-streaming — extracted reasoning appends to existing reasoning_content."""
        proc = ReasoningExtractProcessor()
        response = {
            "choices": [
                {
                    "message": {
                        "content": "<mm:think>more</mm:think>done",
                        "reasoning_content": "seed ",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        result = proc.process_response(response)
        msg = result["choices"][0]["message"]
        assert msg["content"] == "done"
        assert msg["reasoning_content"] == "seed more"

    def test_r20_multiple_blocks(self):
        """R-20: Multiple reasoning blocks interleaved with content."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk(
                    "a<mm:think>r1</mm:think>b<mm:think>r2</mm:think>c", model="minimax-m3"
                )
            ],
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "r1r2"
        assert _concat_content(chunks) == "abc"

    def test_r21_literal_think_in_answer_does_not_swallow_content(self):
        """R-21: a literal <think> in the answer (post-reasoning) stays content, no cascade."""
        proc = ReasoningExtractProcessor()
        emitted, flushed = _feed_stream(
            proc,
            [
                _make_stream_chunk("<mm:think>", model="minimax-m3"),
                _make_stream_chunk("real reasoning"),
                _make_stream_chunk("</mm:think>"),
                _make_stream_chunk("Use <think> like this, then more text."),
            ],
        )
        chunks = emitted + flushed
        assert _concat_reasoning(chunks) == "real reasoning"
        assert _concat_content(chunks) == "Use <think> like this, then more text."


class TestPendingTagLen:
    """Tests for the _pending_tag_len partial-tag holdback helper."""

    def test_open_tag_prefix_held(self):
        """A tail equal to a proper prefix of an opening tag is held back."""
        assert _pending_tag_len("foo<mm:th", ("<mm:think>", "<think>")) == len("<mm:th")

    def test_lone_bracket_held(self):
        """A trailing '<' could start any tag, so it is held back."""
        assert _pending_tag_len("x<", ("<mm:think>", "<think>")) == 1

    def test_non_prefix_not_held(self):
        """A '<' that is not a tag prefix (e.g. 'a < b') is not held back."""
        assert _pending_tag_len("a < b", ("<mm:think>", "<think>")) == 0

    def test_complete_tag_not_held(self):
        """A fully-present tag is matched by str.find, so nothing is held back for it."""
        assert _pending_tag_len("<think>", ("<think>",)) == 0

    def test_empty_text(self):
        """Empty text holds back nothing."""
        assert _pending_tag_len("", ("<think>",)) == 0
