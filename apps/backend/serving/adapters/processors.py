"""Output processors for converting model-specific formats to OpenAI standard.

This module implements the Strategy pattern to handle different model output formats.
It separates the complex parsing logic from the network adapters.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: The most tool-call text the GLM and Qwen Coder processors hold back while a
#: stream runs, waiting for its end to parse the call. As much as the SSE parser
#: lets one frame carry (``serving.servers.sse.MAX_FRAME_CHARS``), and many times
#: a model's longest answer. Past it the text is no tool call a client can use:
#: it is sent on as content, as a call that fails to parse is at the end.
MAX_TOOL_CALL_CHARS = 16 * 1024 * 1024


class BaseProcessor(ABC):
    """Base class for output processors."""

    @abstractmethod
    def process_stream_chunk(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Process a streaming chunk.

        Returns a list of chunks to yield (0, 1, or more).
        This allows buffering (return []) and expansion (return multiple chunks).
        """
        pass

    def flush(self) -> list[dict[str, Any]]:
        """Flush any buffered content at the end of the stream."""
        return []

    @abstractmethod
    def process_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Process a non-streaming response."""
        pass


class DefaultProcessor(BaseProcessor):
    """Pass-through processor for standard OpenAI-compatible models."""

    def process_stream_chunk(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Process a streaming chunk without modification.

        Args:
            chunk: The streaming chunk to process.

        Returns:
            A list containing the unmodified chunk.
        """
        return [chunk]

    def process_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Process a non-streaming response without modification.

        Args:
            response: The response to process.

        Returns:
            The unmodified response.
        """
        return response


class GLMProcessor(BaseProcessor):
    """Processor for GLM-4 models that output XML tags.

    Simplifies output by:
    1. Stripping <think> tags (keeping content).
    2. Buffering and converting <tool_call> XML to OpenAI JSON tool_calls.
    """

    def __init__(self) -> None:
        # Text mode's tail: at most the last 19 characters, held while they may
        # still become a tag.
        self.buffer = ""
        self.in_tool_mode = False
        self.model_id = "glm-4"  # Default placeholder
        self.upstream_finish_reason: str | None = None
        # Tool mode's text, from ``<tool_call>`` on.
        self._tool_text = _HeldText()

    def process_stream_chunk(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Process a streaming chunk from GLM-4 models.

        Handles XML tag stripping and tool call buffering.

        Args:
            chunk: The streaming chunk to process.

        Returns:
            A list of processed chunks (may be empty if buffering).
        """
        choices = chunk.get("choices", [])
        if not choices:
            return [chunk]

        fr = choices[0].get("finish_reason")
        if fr:
            self.upstream_finish_reason = fr

        delta = choices[0].get("delta", {})
        content = delta.get("content", "")

        # If content is None (e.g. internal keepalive), handle gracefully
        if content is None:
            return [chunk]

        self.model_id = chunk.get("model", self.model_id)

        to_yield = []

        if self.in_tool_mode:
            # We are in tool mode. We buffer EVERYTHING until the stream ends.
            # We DO NOT emit any content chunks while in tool mode.
            to_yield.extend(self._hold_tool_text(chunk, content))
        elif "<tool_call>" in (buffered := self.buffer + content):
            self.in_tool_mode = True
            # Emit any text before <tool_call> (with think tags stripped)
            idx = buffered.index("<tool_call>")
            pre_text = buffered[:idx].replace("<think>", "").replace("</think>", "")
            self.buffer = ""
            if pre_text:
                new_chunk = _clone_chunk(chunk)
                new_chunk["choices"][0]["delta"]["content"] = pre_text
                to_yield.append(new_chunk)
            to_yield.extend(self._hold_tool_text(chunk, buffered[idx:]))
        else:
            self.buffer = buffered
            # Regular text mode (thinking or normal response)
            # We want to emit text as it comes, but handle <think> tags.

            # Safety: Don't emit the end of the buffer if it looks like a partial tag start
            safe_index = len(self.buffer)
            last_open = self.buffer.rfind("<")
            if last_open != -1 and last_open > len(self.buffer) - 20:
                # If there is a '<' near the end, wait to see if it becomes a tag
                safe_index = last_open

            text_to_emit = self.buffer[:safe_index]
            self.buffer = self.buffer[safe_index:]

            # Strip tags
            text_to_emit = text_to_emit.replace("<think>", "").replace("</think>", "")

            if text_to_emit:
                # Create a new chunk with sanitized content
                new_chunk = _clone_chunk(chunk)
                new_chunk["choices"][0]["delta"]["content"] = text_to_emit
                to_yield.append(new_chunk)

        if not to_yield and _has_stream_signals(chunk):
            # The chunk was fully buffered/empty but carries finish_reason or
            # usage -- forward those signals so they aren't lost.
            to_yield.append(_signal_only_chunk(chunk))

        return to_yield

    def _hold_tool_text(self, chunk: dict[str, Any], text: str) -> list[dict[str, Any]]:
        """Hold *text* of the tool call, unless the call has grown past any real one.

        Past :data:`MAX_TOOL_CALL_CHARS` what is held is sent on as content, as
        :meth:`flush` sends a call it cannot parse, and the processor reads text
        again: holding it to the end of the stream would keep every byte an
        upstream that never closes the call sends.
        """
        self._tool_text.add(text)
        if self._tool_text.chars <= MAX_TOOL_CALL_CHARS:
            return []
        self.in_tool_mode = False
        return [_tool_text_overflow(chunk, self._tool_text.take(), "glm")]

    def flush(self) -> list[dict[str, Any]]:
        """Called at end of stream. Process any remaining buffer."""
        if not self.in_tool_mode and not self.buffer:
            return []

        to_yield = []

        if self.in_tool_mode:
            # We have a buffered tool call string. Parse it!
            xml_text = self._tool_text.take()
            tool_calls = self._parse_glm_tool_xml(xml_text)
            if tool_calls:
                # Emit a chunk with tool_calls.
                # IMPORTANT: Set finish_reason to "tool_calls" to override
                # upstream "stop" -- but never mask a truncation: a tool call
                # cut off at max_tokens must stay "length" so the client sees
                # truncated arguments rather than a spuriously complete call.
                if self.upstream_finish_reason in (None, "", "stop"):
                    finish_reason = "tool_calls"
                else:
                    finish_reason = self.upstream_finish_reason
                chunk = {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": tool_calls, "content": None},
                            "finish_reason": finish_reason,
                        }
                    ],
                }
                to_yield.append(chunk)
            else:
                # Fallback: if parsing failed, just emit the raw buffer as content
                # This helps debugging if we guessed wrong about it being a tool
                chunk = {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [
                        {"index": 0, "delta": {"content": xml_text}, "finish_reason": None}
                    ],
                }
                to_yield.append(chunk)
        else:
            # Flush remaining text (e.g. closing tags or text after last safe_index)
            text = self.buffer.replace("<think>", "").replace("</think>", "")
            if text:
                chunk = {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                to_yield.append(chunk)

        self.buffer = ""
        self.in_tool_mode = False
        self.upstream_finish_reason = None
        return to_yield

    def _parse_glm_tool_xml(self, xml_text: str) -> list[dict[str, Any]] | None:
        """Parse GLM tool XML into OpenAI tool_calls format.

        Supports multiple ``<tool_call>`` blocks (parallel tool calls): args
        are harvested per block, not from the whole buffer, so one call's
        arguments never leak into another's.
        """
        # Split the buffer into one segment per <tool_call> block. The last
        # block may lack its closing tag (stream cut off), so split on the
        # opening tag rather than requiring </tool_call>.
        segments = [seg for seg in xml_text.split("<tool_call>")[1:] if seg.strip()]
        if not segments:
            return None

        tool_calls: list[dict[str, Any]] = []
        for segment in segments:
            # Tool name: text after <tool_call> and before the next tag/newline.
            # Format varies: <tool_call>NAME</tool_call>... or <tool_call>NAME\n<arg_key>...
            name_match = re.match(r"\s*([^<\n]+)", segment)
            if not name_match:
                continue
            name = name_match.group(1).strip()
            if not name:
                continue

            # Args: GLM usually emits <arg_key>K</arg_key><arg_value>V</arg_value>
            # Matched as pairs, not as two independent scans zipped together.
            # A value may legitimately contain the marker text -- a coding agent
            # asking to run `echo "<arg_key>x</arg_key>"` is enough -- and the
            # key scan would then pick up that nested marker while the value
            # scan did not, leaving the two lists different lengths.
            # ``zip(..., strict=False)`` silently shifts every later pair rather
            # than raising, so the request went upstream with argument names
            # bound to the wrong values.
            args = {}
            for k, v in _glm_arg_pairs(segment):
                k = k.strip()
                v = v.strip()
                # OpenAI expects 'arguments' to be a JSON string of the whole
                # object; GLM gives us separate keys, so build the dict and dump
                # it. GLM sometimes puts JSON in the value:
                # <arg_value>["bash", "-lc", ...]</arg_value>
                try:
                    if (v.startswith("[") and v.endswith("]")) or (
                        v.startswith("{") and v.endswith("}")
                    ):
                        args[k] = json.loads(v)
                    else:
                        args[k] = v
                except Exception:
                    args[k] = v

            tool_calls.append(
                {
                    "index": len(tool_calls),
                    "id": f"call_glm_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            )

        return tool_calls or None

    def process_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Process non-streaming response."""
        # Simple cleanup for non-streaming
        choices = response.get("choices", [])
        if not choices:
            return response

        message = choices[0].get("message", {})
        content = message.get("content", "")

        if content:
            content = content.replace("<think>", "").replace("</think>", "")
            message["content"] = content

        return response


class QwenCoderProcessor(BaseProcessor):
    """Processor for Qwen3-Coder models that output custom XML tool calls.

    Qwen3-Coder uses a non-standard XML format:
        <tool_call>
        <function=function_name>
        <parameter=param_name>value</parameter>
        </function>
        </tool_call>

    This processor buffers and converts these to OpenAI JSON tool_calls.
    Text before the first <tool_call> is emitted as regular content.
    Multiple <tool_call> blocks are supported (parallel tool calls).

    The XML is read with ``str.find`` scans (:func:`_qwen_tool_blocks`,
    :func:`_qwen_function`, :func:`_qwen_parameters`) that match exactly what
    the regular expressions they replace matched. Those regexes searched the
    rest of the text from every unclosed tag, so a reply of many unclosed tags
    cost time quadratic in its length: about 18 s for 214 KiB.
    """

    def __init__(self) -> None:
        # Text mode's tail: at most the last 19 characters, held while they may
        # still become a tag.
        self.buffer = ""
        self.in_tool_mode = False
        self.model_id = "qwen3-coder"
        self.upstream_finish_reason: str | None = None
        # Tool mode's text, from the first ``<tool_call>`` on.
        self._tool_text = _HeldText()

    def process_stream_chunk(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Process a streaming chunk from Qwen3-Coder models.

        Buffers content when tool call XML is detected. Emits regular text
        immediately (with partial-tag safety). Passes through native tool_calls
        untouched.
        """
        choices = chunk.get("choices", [])
        if not choices:
            return [chunk]

        fr = choices[0].get("finish_reason")
        if fr:
            self.upstream_finish_reason = fr

        delta = choices[0].get("delta", {})

        # Pass through native tool_calls untouched (but ignore null/empty)
        if delta.get("tool_calls"):
            return [chunk]

        to_yield = self._process_content(chunk, delta.get("content"))

        if not to_yield and _has_stream_signals(chunk):
            # The chunk was fully buffered/empty but carries finish_reason or
            # usage -- forward those signals so they aren't lost.
            to_yield.append(_signal_only_chunk(chunk))

        return to_yield

    def _process_content(self, chunk: dict[str, Any], content: Any) -> list[dict[str, Any]]:
        """Buffer/emit a chunk's text content; returns chunks to yield."""
        if not isinstance(content, str):
            return []

        self.model_id = chunk.get("model", self.model_id)

        if self.in_tool_mode:
            # Buffer everything until flush
            return self._hold_tool_text(chunk, content)

        self.buffer += content

        # Check if we should enter tool mode
        if "<tool_call>" in self.buffer:
            self.in_tool_mode = True
            # Emit any text before the first <tool_call> tag
            idx = self.buffer.index("<tool_call>")
            pre_text = self.buffer[:idx].strip()
            held = self.buffer[idx:]
            self.buffer = ""

            to_yield = []
            if pre_text:
                new_chunk = _clone_chunk(chunk)
                new_chunk["choices"][0]["delta"]["content"] = pre_text
                to_yield.append(new_chunk)
            to_yield.extend(self._hold_tool_text(chunk, held))
            return to_yield

        # Regular text mode — emit with partial-tag safety
        safe_index = len(self.buffer)
        last_open = self.buffer.rfind("<")
        if last_open != -1 and last_open > len(self.buffer) - 20:
            safe_index = last_open

        text_to_emit = self.buffer[:safe_index]
        self.buffer = self.buffer[safe_index:]

        if text_to_emit:
            new_chunk = _clone_chunk(chunk)
            new_chunk["choices"][0]["delta"]["content"] = text_to_emit
            return [new_chunk]

        return []

    def _hold_tool_text(self, chunk: dict[str, Any], text: str) -> list[dict[str, Any]]:
        """Hold *text* of the tool call, unless the call has grown past any real one.

        See :meth:`GLMProcessor._hold_tool_text`.
        """
        self._tool_text.add(text)
        if self._tool_text.chars <= MAX_TOOL_CALL_CHARS:
            return []
        self.in_tool_mode = False
        return [_tool_text_overflow(chunk, self._tool_text.take(), "qwen_coder")]

    def flush(self) -> list[dict[str, Any]]:
        """Flush buffered content, parsing any tool call XML."""
        if not self.in_tool_mode and not self.buffer:
            return []

        to_yield: list[dict[str, Any]] = []

        if self.in_tool_mode:
            xml_text = self._tool_text.take()
            tool_calls = self._parse_qwen_tool_xml(xml_text)
            if tool_calls:
                # Report "tool_calls" over an upstream "stop", but never mask a
                # truncation: a tool call cut off at max_tokens must stay
                # "length" so the client sees truncated arguments rather than a
                # spuriously complete call.
                if self.upstream_finish_reason in (None, "", "stop"):
                    finish_reason = "tool_calls"
                else:
                    finish_reason = self.upstream_finish_reason
                chunk = {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": tool_calls, "content": None},
                            "finish_reason": finish_reason,
                        }
                    ],
                }
                to_yield.append(chunk)
            else:
                # Parsing failed — emit raw buffer as content for debugging
                chunk = {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [
                        {"index": 0, "delta": {"content": xml_text}, "finish_reason": None}
                    ],
                }
                to_yield.append(chunk)
        else:
            text = self.buffer.strip()
            if text:
                chunk = {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                to_yield.append(chunk)

        self.buffer = ""
        self.in_tool_mode = False
        self.upstream_finish_reason = None
        return to_yield

    def _parse_qwen_tool_xml(self, xml_text: str) -> list[dict[str, Any]] | None:
        """Parse Qwen3-Coder tool XML into OpenAI tool_calls format."""
        tool_calls: list[dict[str, Any]] = []

        # Find all <tool_call>...</tool_call> blocks
        blocks = _qwen_tool_blocks(xml_text)
        if not blocks:
            # Fallback: try matching unclosed blocks (stream may lack closing tag)
            if "<function=" in xml_text:
                blocks = [xml_text]
            else:
                return None

        for i, block in enumerate(blocks):
            function = _qwen_function(block)
            if function is None:
                continue

            func_name = function[0].strip()
            params_str = function[1]

            # Parse parameters
            args: dict[str, Any] = {}
            for param_name, param_value in _qwen_parameters(params_str):
                param_name = param_name.strip()

                # Strip leading/trailing newlines (Qwen3-Coder adds these)
                if param_value.startswith("\n"):
                    param_value = param_value[1:]
                if param_value.endswith("\n"):
                    param_value = param_value[:-1]

                # Try JSON parsing for structured values
                if (param_value.startswith("[") and param_value.endswith("]")) or (
                    param_value.startswith("{") and param_value.endswith("}")
                ):
                    try:
                        args[param_name] = json.loads(param_value)
                    except (json.JSONDecodeError, ValueError):
                        args[param_name] = param_value
                else:
                    args[param_name] = param_value

            tool_calls.append(
                {
                    "index": i,
                    "id": f"call_qwen_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": func_name, "arguments": json.dumps(args)},
                }
            )

        return tool_calls if tool_calls else None

    def process_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Process non-streaming response, converting XML tool calls to JSON."""
        choices = response.get("choices", [])
        if not choices:
            return response

        message = choices[0].get("message", {})
        content = message.get("content", "")

        if not content or not isinstance(content, str):
            return response

        if "<tool_call>" in content:
            tool_calls = self._parse_qwen_tool_xml(content)
            if tool_calls:
                # Extract text before the first <tool_call> as content
                first_tc = content.find("<tool_call>")
                pre_text = content[:first_tc].strip() if first_tc > 0 else None
                message["tool_calls"] = tool_calls
                message["content"] = pre_text
                choices[0]["finish_reason"] = "tool_calls"

        return response


class ThinkBlockProcessor(BaseProcessor):
    r"""Processor that strips entire <think>...</think> blocks (tags + content).

    Used for models (e.g. MiniMax) where thinking output should be fully hidden
    from the client, unlike GLMProcessor which only strips the tags.

    Chunks containing native tool_calls are passed through unmodified.

    Blocks are found with ``str.find`` scans that move only forward. The regex
    they replace, ``<think>[\s\S]*?</think>``, searched the rest of the text
    from every unclosed ``<think>``: a reply of many cost time quadratic in its
    length, about 15 s for 136 KiB.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.in_think = False
        self.model_id = "unknown"

    def process_stream_chunk(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Strip <think> blocks from streaming chunks, pass through tool_calls."""
        choices = chunk.get("choices", [])
        if not choices:
            return [chunk]

        delta = choices[0].get("delta", {})

        # Pass through native tool_calls and reasoning fields untouched (but ignore null/empty).
        # This processor only strips literal <think> blocks embedded in content.
        if delta.get("tool_calls"):
            return [chunk]
        if delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking"):
            return [chunk]

        # Preserve terminal usage-bearing chunks even when they carry no text.
        if chunk.get("usage") is not None:
            return [chunk]

        content = delta.get("content")

        if not isinstance(content, str):
            if _has_stream_signals(chunk):
                # Forward finish_reason/usage even when there is no text.
                return [_signal_only_chunk(chunk)]
            return []

        if not content and chunk.get("usage"):
            return [chunk]

        # What is left of the held tail and this chunk is ``buffer[pos:]``: the
        # loop moves ``pos`` on instead of re-slicing the rest after each tag,
        # which copied it once per tag.
        buffer = self.buffer + content
        pos = 0
        self.model_id = chunk.get("model", self.model_id)

        to_yield: list[dict[str, Any]] = []

        while True:
            if self.in_think:
                end_idx = buffer.find("</think>", pos)
                if end_idx != -1:
                    # Discard everything up to and including </think>
                    pos = end_idx + 8
                    self.in_think = False
                else:
                    # Keep tail for partial </think> detection
                    pos = max(pos, len(buffer) - 8)
                    break
            else:
                start_idx = buffer.find("<think>", pos)
                if start_idx != -1:
                    # Emit text before <think>
                    if start_idx > pos:
                        new_chunk = _clone_chunk(chunk)
                        new_chunk["choices"][0]["delta"]["content"] = buffer[pos:start_idx]
                        to_yield.append(new_chunk)
                    pos = start_idx + 7
                    self.in_think = True
                else:
                    # No <think> tag found — emit safe portion
                    safe_index = len(buffer)
                    last_open = buffer.rfind("<", pos)
                    if last_open != -1 and last_open > len(buffer) - 8:
                        safe_index = last_open

                    if safe_index > pos:
                        new_chunk = _clone_chunk(chunk)
                        new_chunk["choices"][0]["delta"]["content"] = buffer[pos:safe_index]
                        to_yield.append(new_chunk)
                    pos = safe_index
                    break

        self.buffer = buffer[pos:]

        if not to_yield and _has_stream_signals(chunk):
            # The chunk was fully buffered/discarded but carries finish_reason
            # or usage -- forward those signals so they aren't lost.
            to_yield.append(_signal_only_chunk(chunk))

        return to_yield

    def flush(self) -> list[dict[str, Any]]:
        """Flush remaining buffer, discarding any incomplete think blocks."""
        if not self.buffer or self.in_think:
            self.buffer = ""
            self.in_think = False
            return []

        text = _strip_closed_blocks(self.buffer, "<think>", "</think>").strip()
        self.buffer = ""

        if not text:
            return []

        return [
            {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": self.model_id,
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
        ]

    def process_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Strip <think> blocks from non-streaming response content."""
        choices = response.get("choices", [])
        if not choices:
            return response

        message = choices[0].get("message", {})
        content = message.get("content", "")

        if content:
            message["content"] = _strip_closed_blocks(content, "<think>", "</think>").strip()

        return response


class ReasoningExtractProcessor(BaseProcessor):
    """Lift inline reasoning tags into the OpenAI reasoning channel.

    Some providers stream a model's chain-of-thought as literal tag-delimited
    text inside ``delta.content`` instead of a dedicated ``reasoning_content``
    delta. MiniMax's OpenAI-compatible endpoint is the motivating case: in
    streaming mode it emits the thinking block as content, e.g.

        content: "<mm:think>" -> "reasoning ..." -> "</mm:think>" -> "answer"

    By default only MiniMax-M3's ``<mm:think>`` pair is recognized: the inner
    text is re-emitted as ``reasoning_content`` deltas and text outside the tag
    streams through as ``content``. ``<think>`` is deliberately NOT matched by
    default -- on an M3 route the model never emits it, so a literal ``<think>``
    in the *answer* (discussing reasoning models, XML, etc.) must stream as
    content, not open a reasoning block that never closes and swallows the rest
    of the reply. Pass ``tag_pairs`` to reuse this processor for a
    ``<think>``-style model.

    Unlike a "reasoning starts open" parser, tags are matched only when actually
    present: a reply with no thinking (no tags) streams its answer verbatim
    rather than being misclassified wholesale as reasoning.

    Structured reasoning deltas (``reasoning_content`` / ``reasoning`` /
    ``thinking``) and native ``tool_calls`` are passed through untouched, and
    terminal usage / ``finish_reason`` signals are preserved.

    Tags are found with ``str.find`` scans that move only forward
    (:func:`_lift_closed_blocks` for a whole response); a regex searching the
    rest of the text from every unclosed tag costs quadratic time.
    """

    # Each pair is (open_tag, close_tag) with close_tag == "</" + inner + ">".
    _DEFAULT_TAG_PAIRS: tuple[tuple[str, str], ...] = (("<mm:think>", "</mm:think>"),)

    def __init__(self, tag_pairs: tuple[tuple[str, str], ...] | None = None) -> None:
        self.tag_pairs = tag_pairs or self._DEFAULT_TAG_PAIRS
        self._open_tags = tuple(open_tag for open_tag, _ in self.tag_pairs)
        # The inner names: a whole response's blocks are ``<name>...</name>``.
        self._block_names = tuple(open_tag[1:-1] for open_tag, _ in self.tag_pairs)
        self.buffer = ""
        self.in_reasoning = False
        self.close_tag = ""
        self.model_id = "unknown"

    def process_stream_chunk(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert inline reasoning-tag content into reasoning_content deltas."""
        choices = chunk.get("choices", [])
        if not choices:
            return [chunk]

        delta = choices[0].get("delta", {})

        # Pass through native tool_calls and already-structured reasoning fields
        # untouched (ignoring null/empty). This processor only lifts literal
        # reasoning tags embedded in content.
        if delta.get("tool_calls"):
            return [chunk]
        if delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking"):
            return [chunk]

        content = delta.get("content")
        if not isinstance(content, str):
            if _has_stream_signals(chunk):
                # Forward finish_reason/usage even when there is no text.
                return [_signal_only_chunk(chunk)]
            return []

        # What is left of the held tail and this chunk is ``buffer[pos:]``: the
        # loop moves ``pos`` on instead of re-slicing the rest after each tag.
        buffer = self.buffer + content
        pos = 0
        opens = [(tag, _Finder(buffer, tag)) for tag in self._open_tags]
        self.model_id = chunk.get("model", self.model_id)

        to_yield: list[dict[str, Any]] = []

        while True:
            if self.in_reasoning:
                end_idx = buffer.find(self.close_tag, pos)
                if end_idx != -1:
                    if end_idx > pos:
                        to_yield.append(self._reasoning_chunk(chunk, buffer[pos:end_idx]))
                    pos = end_idx + len(self.close_tag)
                    self.in_reasoning = False
                    self.close_tag = ""
                    continue
                # Closing tag not seen yet: stream the reasoning so far, holding
                # back a possible partial close tag at the tail.
                rest = buffer[pos:]
                hold = _pending_tag_len(rest, (self.close_tag,))
                if len(rest) > hold:
                    to_yield.append(self._reasoning_chunk(chunk, rest[: len(rest) - hold]))
                self.buffer = rest[len(rest) - hold :]
                break

            open_idx, open_tag = _earliest_tag(opens, pos)
            if open_idx != -1:
                if open_idx > pos:
                    to_yield.append(self._content_chunk(chunk, buffer[pos:open_idx]))
                pos = open_idx + len(open_tag)
                self.in_reasoning = True
                self.close_tag = self._close_for(open_tag)
                continue
            # No opening tag: stream content, holding back a possible partial
            # opening tag at the tail.
            rest = buffer[pos:]
            hold = _pending_tag_len(rest, self._open_tags)
            if len(rest) > hold:
                to_yield.append(self._content_chunk(chunk, rest[: len(rest) - hold]))
            self.buffer = rest[len(rest) - hold :]
            break

        if _has_stream_signals(chunk):
            # finish_reason / usage always ride a dedicated trailing chunk, never
            # an intermediate split delta (which would surface a premature finish
            # or duplicate usage). _signal_only_chunk empties the delta.
            to_yield.append(_signal_only_chunk(chunk))

        return to_yield

    def flush(self) -> list[dict[str, Any]]:
        """Emit any buffered tail at end of stream.

        A reasoning block left unclosed (upstream truncated mid-thought) is
        surfaced as reasoning_content rather than dropped; a held-back partial
        opening tag that never completed is emitted as content.
        """
        if not self.buffer:
            return []
        text = self.buffer
        self.buffer = ""
        if self.in_reasoning:
            self.in_reasoning = False
            close_tag = self.close_tag
            self.close_tag = ""
            # The tail held mid-stream can be a truncated close tag (e.g. "</mm")
            # rather than reasoning; drop that fragment instead of emitting it.
            hold = _pending_tag_len(text, (close_tag,))
            text = text[: len(text) - hold]
            if text:
                return [self._standalone_chunk({"reasoning_content": text})]
            return []
        return [self._standalone_chunk({"content": text})]

    def process_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Lift inline reasoning tags out of non-streaming response content."""
        choices = response.get("choices", [])
        if not choices:
            return response

        message = choices[0].get("message", {})
        content = message.get("content", "")
        if not content or not isinstance(content, str):
            return response

        new_content, reasonings = _lift_closed_blocks(content, self._block_names)
        # An unterminated opening tag (e.g. a reply truncated at max_tokens
        # inside the reasoning block) has no closing tag for the pair regex to
        # match: treat everything from the opening tag onward as reasoning,
        # mirroring the streaming flush().
        open_idx, open_tag = self._find_open(new_content)
        if open_idx != -1:
            tail = new_content[open_idx + len(open_tag) :]
            # Drop a trailing partial close tag from a truncated reasoning block.
            hold = _pending_tag_len(tail, (self._close_for(open_tag),))
            reasonings.append(tail[: len(tail) - hold])
            new_content = new_content[:open_idx]

        if reasonings:
            message["content"] = new_content.strip()
            joined = "".join(reasonings).strip()
            existing = message.get("reasoning_content")
            message["reasoning_content"] = f"{existing}{joined}" if existing else joined

        return response

    def _find_open(self, buffer: str) -> tuple[int, str]:
        """Return (index, tag) of the earliest opening tag, or (-1, "")."""
        best_idx, best_tag = -1, ""
        for tag in self._open_tags:
            idx = buffer.find(tag)
            if idx == -1:
                continue
            if best_idx == -1 or idx < best_idx or (idx == best_idx and len(tag) > len(best_tag)):
                best_idx, best_tag = idx, tag
        return best_idx, best_tag

    def _close_for(self, open_tag: str) -> str:
        """Return the closing tag paired with *open_tag*."""
        for known_open_tag, close_tag in self.tag_pairs:
            if known_open_tag == open_tag:
                return close_tag
        return "</think>"

    def _content_chunk(self, chunk: dict[str, Any], text: str) -> dict[str, Any]:
        """Clone *chunk* carrying *text* as the sole content delta.

        Stream signals (usage / finish_reason) are stripped: an intermediate
        split delta must never carry them (they ride a trailing signal chunk).
        """
        new_chunk = _clone_chunk(chunk)
        new_chunk.pop("usage", None)
        new_chunk["choices"][0]["finish_reason"] = None
        new_delta = new_chunk["choices"][0]["delta"]
        for field in ("reasoning_content", "reasoning", "thinking"):
            new_delta.pop(field, None)
        new_delta["content"] = text
        return new_chunk

    def _reasoning_chunk(self, chunk: dict[str, Any], text: str) -> dict[str, Any]:
        """Clone *chunk* carrying *text* as the sole reasoning_content delta.

        Stream signals (usage / finish_reason) are stripped: an intermediate
        split delta must never carry them (they ride a trailing signal chunk).
        """
        new_chunk = _clone_chunk(chunk)
        new_chunk.pop("usage", None)
        new_chunk["choices"][0]["finish_reason"] = None
        new_delta = new_chunk["choices"][0]["delta"]
        new_delta.pop("content", None)
        new_delta["reasoning_content"] = text
        return new_chunk

    def _standalone_chunk(self, delta: dict[str, Any]) -> dict[str, Any]:
        """Build a fresh chunk carrying *delta* (used by flush)."""
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.model_id,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }


def _clone_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """Deep copy structure of a chunk for modification."""
    new_chunk = chunk.copy()
    if "choices" in chunk:
        new_chunk["choices"] = [c.copy() for c in chunk["choices"]]
        for _i, c in enumerate(new_chunk["choices"]):
            if "delta" in c:
                c["delta"] = c["delta"].copy()
    return new_chunk


def _has_stream_signals(chunk: dict[str, Any]) -> bool:
    """True if *chunk* carries stream-level signals (finish_reason / usage)."""
    if chunk.get("usage") is not None:
        return True
    return any(c.get("finish_reason") for c in chunk.get("choices") or [])


def _signal_only_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """Copy of *chunk* with the delta emptied but finish_reason/usage intact.

    Buffering processors swallow content chunks; when a swallowed chunk also
    carries the upstream ``finish_reason`` (e.g. "length" on truncation) or a
    ``usage`` object attached to a choice-bearing chunk, those signals must
    still reach the adapter's bookkeeping or the final chunk misreports the
    finish reason and falls back to estimated usage.
    """
    new_chunk = _clone_chunk(chunk)
    for c in new_chunk.get("choices") or []:
        c["delta"] = {}
    return new_chunk


def _pending_tag_len(text: str, tags: tuple[str, ...]) -> int:
    """Length of the longest tail of *text* that is a proper prefix of a *tag*.

    Held back from emission so a tag split across chunk boundaries is not
    surfaced as content (or reasoning) before it can be recognized as a tag.
    """
    best = 0
    for tag in tags:
        limit = min(len(text), len(tag) - 1)
        for length in range(limit, best, -1):
            if tag.startswith(text[-length:]):
                best = length
                break
    return best


# -- Reading model output in time linear in its length -------------------------
#
# The processors used regular expressions with a lazy ``.*?`` between an opening
# and a closing tag. Where the closing tag never comes, the regex engine scans
# the rest of the text from every opening tag before it gives up on it, so a
# reply of many unclosed tags costs time quadratic in its length (the GLM
# argument pattern, cubic): seconds of event loop for a reply of a hundred
# KiB, which a caller can ask a model to write. The scans below find the same
# matches with ``str.find`` and only ever move forward.


class _HeldText:
    """Text held as the pieces it arrived in, joined once, when it is taken.

    Appending each piece to one string an attribute holds copies all of it
    every time, which is quadratic in the text: a GLM tool call of 781 KiB
    streamed in 4-character pieces took 8 s.
    """

    __slots__ = ("_pieces", "chars")

    def __init__(self) -> None:
        self._pieces: list[str] = []
        self.chars = 0

    def add(self, text: str) -> None:
        """Hold *text* after what is already held."""
        self._pieces.append(text)
        self.chars += len(text)

    def take(self) -> str:
        """Return the held text, and hold nothing."""
        text = "".join(self._pieces)
        self._pieces = []
        self.chars = 0
        return text


def _tool_text_overflow(chunk: dict[str, Any], text: str, processor: str) -> dict[str, Any]:
    """Send a held tool call on as content: it ran past :data:`MAX_TOOL_CALL_CHARS`."""
    logger.warning(
        "tool_call_text_overflow",
        extra={
            "event": "tool_call_text_overflow",
            "processor": processor,
            "chars": len(text),
            "limit": MAX_TOOL_CALL_CHARS,
        },
    )
    new_chunk = _clone_chunk(chunk)
    new_chunk["choices"][0]["delta"]["content"] = text
    return new_chunk


class _Finder:
    """``text.find(needle, start)``, answered from the last search while it still holds.

    A scan that looks for the next of several tags at each step would otherwise
    search past a distant occurrence of one of them again at every step. With
    starts that only move forward, each needle's search reads the text once.
    """

    __slots__ = ("_found", "_from", "_needle", "_text")

    def __init__(self, text: str, needle: str) -> None:
        self._text = text
        self._needle = needle
        self._from = -1
        self._found = -1

    def at(self, start: int) -> int:
        """Return the first occurrence of the needle at or after *start*, or -1."""
        if not (0 <= self._from <= start and (self._found == -1 or self._found >= start)):
            self._from = start
            self._found = self._text.find(self._needle, start)
        return self._found


def _earliest_tag(finders: list[tuple[str, _Finder]], start: int) -> tuple[int, str]:
    """Return (index, tag) of the earliest tag at or after *start*, or (-1, "").

    A tie goes to the longer tag, as in :meth:`ReasoningExtractProcessor._find_open`.
    """
    best_idx, best_tag = -1, ""
    for tag, finder in finders:
        idx = finder.at(start)
        if idx == -1:
            continue
        if best_idx == -1 or idx < best_idx or (idx == best_idx and len(tag) > len(best_tag)):
            best_idx, best_tag = idx, tag
    return best_idx, best_tag


def _strip_closed_blocks(text: str, open_tag: str, close_tag: str) -> str:
    r"""Remove each block from *open_tag* to the first *close_tag* after it.

    Exactly ``re.sub(open_tag + r"[\s\S]*?" + close_tag, "", text)``. Once no
    closing tag follows an opening tag, none follows a later one either, so the
    scan stops there.
    """
    kept: list[str] = []
    pos = 0
    while (start := text.find(open_tag, pos)) != -1:
        end = text.find(close_tag, start + len(open_tag))
        if end == -1:
            break
        kept.append(text[pos:start])
        pos = end + len(close_tag)
    if not kept:
        return text
    kept.append(text[pos:])
    return "".join(kept)


def _lift_closed_blocks(text: str, names: tuple[str, ...]) -> tuple[str, list[str]]:
    r"""Take each ``<name>...</name>`` block out of *text*: (the rest, the blocks' insides).

    Exactly what ``re.sub(r"<(name|...)>([\s\S]*?)</\1>", ...)`` takes out: a
    block runs from an opening tag to the first closing tag of its name, and
    the earliest opening tag with one wins, the first-listed name on a tie. A
    name whose next opening tag has no closing tag after it has none after any
    later one either, so it is not looked for again.
    """
    opens = [_Finder(text, f"<{name}>") for name in names]
    closes = [_Finder(text, f"</{name}>") for name in names]
    live = list(range(len(names)))
    kept: list[str] = []
    insides: list[str] = []
    pos = 0
    while live:
        best = chosen = -1
        for i in list(live):
            start = opens[i].at(pos)
            if start == -1 or closes[i].at(start + len(names[i]) + 2) == -1:
                live.remove(i)
            elif best == -1 or start < best:
                best, chosen = start, i
        if chosen == -1:
            break
        inside_from = best + len(names[chosen]) + 2
        end = closes[chosen].at(inside_from)
        kept.append(text[pos:best])
        insides.append(text[inside_from:end])
        pos = end + len(names[chosen]) + 3
    kept.append(text[pos:])
    return "".join(kept), insides


_ARG_VALUE_OPEN_RE = re.compile(r"\s*<arg_value>")


def _glm_arg_pairs(segment: str) -> list[tuple[str, str]]:
    r"""Return the ``(key, value)`` pairs of a GLM tool call, in order.

    Exactly ``re.findall(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>",
    segment, re.DOTALL)``. A key runs to the first ``</arg_key>`` that a value
    follows, and the value to the first ``</arg_value>`` after it. If no key's
    end is followed by a value, or the value never closes, no later key fares
    better, so the scan stops there; the regex instead retried every later
    ``</arg_key>`` from every ``<arg_key>``, cubic in the call: 13 KB took 3.5 s.
    """
    pairs: list[tuple[str, str]] = []
    pos = 0
    while (start := segment.find("<arg_key>", pos)) != -1:
        key_from = start + len("<arg_key>")
        key_end = segment.find("</arg_key>", key_from)
        value_open = None
        while key_end != -1:
            value_open = _ARG_VALUE_OPEN_RE.match(segment, key_end + len("</arg_key>"))
            if value_open is not None:
                break
            key_end = segment.find("</arg_key>", key_end + 1)
        if value_open is None:
            break
        value_end = segment.find("</arg_value>", value_open.end())
        if value_end == -1:
            break
        pairs.append((segment[key_from:key_end], segment[value_open.end() : value_end]))
        pos = value_end + len("</arg_value>")
    return pairs


def _end_anchor(text: str, start: int) -> int:
    """Return where ``$`` (without MULTILINE) first matches in *text* at or after *start*.

    ``$`` matches at the end of the text, and also just before a newline that ends it.
    """
    end = len(text)
    if end > start and text[end - 1] == "\n":
        return end - 1
    return end


def _qwen_tool_blocks(text: str) -> list[str]:
    """Return the inside of each ``<tool_call>...</tool_call>`` block, in order.

    Exactly ``re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)``.
    """
    blocks: list[str] = []
    pos = 0
    while (start := text.find("<tool_call>", pos)) != -1:
        end = text.find("</tool_call>", start + len("<tool_call>"))
        if end == -1:
            break
        blocks.append(text[start + len("<tool_call>") : end])
        pos = end + len("</tool_call>")
    return blocks


def _qwen_function(block: str) -> tuple[str, str] | None:
    """Return ``(name, body)`` of the first ``<function=name>`` in *block*, or None.

    Exactly the groups of ``re.search(r"<function=([^>]+)>(.*?)(?:</function>|$)",
    block, re.DOTALL)``: a name of at least one character up to the first ``>``,
    and a body up to the first ``</function>``, else the end.
    """
    pos = 0
    while (start := block.find("<function=", pos)) != -1:
        name_from = start + len("<function=")
        name_end = block.find(">", name_from)
        if name_end == -1:
            # Nor is there one after any later <function=.
            return None
        if name_end > name_from:
            body_end = block.find("</function>", name_end + 1)
            if body_end == -1:
                body_end = _end_anchor(block, name_end + 1)
            return block[name_from:name_end], block[name_end + 1 : body_end]
        pos = start + 1
    return None


def _qwen_parameters(text: str) -> list[tuple[str, str]]:
    """Return ``(name, value)`` of each ``<parameter=name>`` in *text*, in order.

    Exactly the groups ``re.finditer(r"<parameter=([^>]+)>(.*?)(?:</parameter>"
    r"|(?=<parameter=)|(?=</function>)|$)", text, re.DOTALL)`` yields: a value
    runs to the first of ``</parameter>`` (which it consumes), the next
    ``<parameter=``, a ``</function>``, and the end.
    """
    closes = _Finder(text, "</parameter>")
    nexts = _Finder(text, "<parameter=")
    function_ends = _Finder(text, "</function>")
    params: list[tuple[str, str]] = []
    pos = 0
    while (start := text.find("<parameter=", pos)) != -1:
        name_from = start + len("<parameter=")
        name_end = text.find(">", name_from)
        if name_end == -1:
            break
        if name_end == name_from:
            pos = start + 1
            continue
        value_from = name_end + 1
        close = closes.at(value_from)
        ends = (close, nexts.at(value_from), function_ends.at(value_from))
        value_end = min([i for i in ends if i != -1] + [_end_anchor(text, value_from)])
        params.append((text[name_from:name_end], text[value_from:value_end]))
        pos = value_end + len("</parameter>") if value_end == close else value_end
    return params


_PROCESSOR_MAP: dict[str, type[BaseProcessor]] = {
    "default": DefaultProcessor,
    "glm": GLMProcessor,
    "qwen_coder": QwenCoderProcessor,
    "think_block": ThinkBlockProcessor,
    "reasoning_extract": ReasoningExtractProcessor,
}


def get_processor(model_id: str | None, override: str | None = None) -> BaseProcessor:
    """Factory function to get the appropriate processor.

    Args:
        model_id: Model identifier for auto-detection.
        override: Explicit processor name (bypasses auto-detection).
                  Values: "default", "glm", "qwen_coder", "think_block",
                  "reasoning_extract".
    """
    if override:
        cls = _PROCESSOR_MAP.get(override)
        if not cls:
            valid = ", ".join(sorted(_PROCESSOR_MAP))
            raise ValueError(f"Unknown processor override '{override}'. Valid values: {valid}")
        return cls()

    if not model_id:
        return DefaultProcessor()

    model_id_lower = model_id.lower()

    # Auto-detect from model ID
    # Note: GLMProcessor is NOT auto-detected. Unified OpenAI-compatible routes
    # assume standard OpenAI chunks unless a route explicitly opts into
    # `processor: glm`.
    if "qwen" in model_id_lower and "coder" in model_id_lower:
        return QwenCoderProcessor()
    if "minimax" in model_id_lower:
        return ThinkBlockProcessor()

    return DefaultProcessor()
