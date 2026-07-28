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
        self.buffer = ""
        self.in_tool_mode = False
        self.model_id = "glm-4"  # Default placeholder
        self.upstream_finish_reason: str | None = None

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

        self.buffer += content
        self.model_id = chunk.get("model", self.model_id)

        to_yield = []

        # Check if we should enter tool mode
        if "<tool_call>" in self.buffer and not self.in_tool_mode:
            self.in_tool_mode = True
            # Emit any text before <tool_call> (with think tags stripped)
            idx = self.buffer.index("<tool_call>")
            pre_text = self.buffer[:idx].replace("<think>", "").replace("</think>", "")
            self.buffer = self.buffer[idx:]
            if pre_text:
                new_chunk = _clone_chunk(chunk)
                new_chunk["choices"][0]["delta"]["content"] = pre_text
                to_yield.append(new_chunk)

        if self.in_tool_mode:
            # We are in tool mode. We buffer EVERYTHING until we see a potential end.
            # We DO NOT emit any content chunks while in tool mode.
            pass
        else:
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

    def flush(self) -> list[dict[str, Any]]:
        """Called at end of stream. Process any remaining buffer."""
        if not self.buffer:
            return []

        to_yield = []

        if self.in_tool_mode:
            # We have a buffered tool call string. Parse it!
            tool_calls = self._parse_glm_tool_xml(self.buffer)
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
                        {"index": 0, "delta": {"content": self.buffer}, "finish_reason": None}
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
            args = {}
            keys = re.findall(r"<arg_key>(.*?)</arg_key>", segment, re.DOTALL)
            values = re.findall(r"<arg_value>(.*?)</arg_value>", segment, re.DOTALL)

            for k, v in zip(keys, values, strict=False):
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
    """

    _TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
    _FUNCTION_RE = re.compile(r"<function=([^>]+)>(.*?)(?:</function>|$)", re.DOTALL)
    _PARAMETER_RE = re.compile(
        r"<parameter=([^>]+)>(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|$)",
        re.DOTALL,
    )

    def __init__(self) -> None:
        self.buffer = ""
        self.in_tool_mode = False
        self.model_id = "qwen3-coder"
        self.upstream_finish_reason: str | None = None

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

        self.buffer += content
        self.model_id = chunk.get("model", self.model_id)

        # Check if we should enter tool mode
        if "<tool_call>" in self.buffer and not self.in_tool_mode:
            self.in_tool_mode = True
            # Emit any text before the first <tool_call> tag
            idx = self.buffer.index("<tool_call>")
            pre_text = self.buffer[:idx].strip()
            self.buffer = self.buffer[idx:]

            if pre_text:
                new_chunk = _clone_chunk(chunk)
                new_chunk["choices"][0]["delta"]["content"] = pre_text
                return [new_chunk]
            return []

        if self.in_tool_mode:
            # Buffer everything until flush
            return []

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

    def flush(self) -> list[dict[str, Any]]:
        """Flush buffered content, parsing any tool call XML."""
        if not self.buffer:
            return []

        to_yield: list[dict[str, Any]] = []

        if self.in_tool_mode:
            tool_calls = self._parse_qwen_tool_xml(self.buffer)
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
                        {"index": 0, "delta": {"content": self.buffer}, "finish_reason": None}
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
        blocks = self._TOOL_CALL_RE.findall(xml_text)
        if not blocks:
            # Fallback: try matching unclosed blocks (stream may lack closing tag)
            if "<function=" in xml_text:
                blocks = [xml_text]
            else:
                return None

        for i, block in enumerate(blocks):
            func_match = self._FUNCTION_RE.search(block)
            if not func_match:
                continue

            func_name = func_match.group(1).strip()
            params_str = func_match.group(2)

            # Parse parameters
            args: dict[str, Any] = {}
            for param_match in self._PARAMETER_RE.finditer(params_str):
                param_name = param_match.group(1).strip()
                param_value = param_match.group(2)

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
    """Processor that strips entire <think>...</think> blocks (tags + content).

    Used for models (e.g. MiniMax) where thinking output should be fully hidden
    from the client, unlike GLMProcessor which only strips the tags.

    Chunks containing native tool_calls are passed through unmodified.
    """

    _THINK_RE = re.compile(r"<think>[\s\S]*?</think>")

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

        self.buffer += content
        self.model_id = chunk.get("model", self.model_id)

        to_yield: list[dict[str, Any]] = []

        while True:
            if self.in_think:
                end_idx = self.buffer.find("</think>")
                if end_idx != -1:
                    # Discard everything up to and including </think>
                    self.buffer = self.buffer[end_idx + 8 :]
                    self.in_think = False
                else:
                    # Keep tail for partial </think> detection
                    if len(self.buffer) > 8:
                        self.buffer = self.buffer[-8:]
                    break
            else:
                start_idx = self.buffer.find("<think>")
                if start_idx != -1:
                    # Emit text before <think>
                    text_before = self.buffer[:start_idx]
                    if text_before:
                        new_chunk = _clone_chunk(chunk)
                        new_chunk["choices"][0]["delta"]["content"] = text_before
                        to_yield.append(new_chunk)
                    self.buffer = self.buffer[start_idx + 7 :]
                    self.in_think = True
                else:
                    # No <think> tag found — emit safe portion
                    safe_index = len(self.buffer)
                    last_open = self.buffer.rfind("<")
                    if last_open != -1 and last_open > len(self.buffer) - 8:
                        safe_index = last_open

                    text_to_emit = self.buffer[:safe_index]
                    self.buffer = self.buffer[safe_index:]

                    if text_to_emit:
                        new_chunk = _clone_chunk(chunk)
                        new_chunk["choices"][0]["delta"]["content"] = text_to_emit
                        to_yield.append(new_chunk)
                    break

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

        text = self._THINK_RE.sub("", self.buffer).strip()
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
            message["content"] = self._THINK_RE.sub("", content).strip()

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
    """

    # Each pair is (open_tag, close_tag) with close_tag == "</" + inner + ">".
    _DEFAULT_TAG_PAIRS: tuple[tuple[str, str], ...] = (("<mm:think>", "</mm:think>"),)

    def __init__(self, tag_pairs: tuple[tuple[str, str], ...] | None = None) -> None:
        self.tag_pairs = tag_pairs or self._DEFAULT_TAG_PAIRS
        self._open_tags = tuple(open_tag for open_tag, _ in self.tag_pairs)
        names = "|".join(re.escape(open_tag[1:-1]) for open_tag, _ in self.tag_pairs)
        self._nonstream_re = re.compile(rf"<({names})>([\s\S]*?)</\1>")
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

        # Preserve terminal usage-bearing chunks even when they carry no text.
        if chunk.get("usage") is not None:
            return [chunk]

        content = delta.get("content")
        if not isinstance(content, str):
            if _has_stream_signals(chunk):
                # Forward finish_reason/usage even when there is no text.
                return [_signal_only_chunk(chunk)]
            return []

        self.buffer += content
        self.model_id = chunk.get("model", self.model_id)

        to_yield: list[dict[str, Any]] = []

        while True:
            if self.in_reasoning:
                end_idx = self.buffer.find(self.close_tag)
                if end_idx != -1:
                    inner = self.buffer[:end_idx]
                    if inner:
                        to_yield.append(self._reasoning_chunk(chunk, inner))
                    self.buffer = self.buffer[end_idx + len(self.close_tag) :]
                    self.in_reasoning = False
                    self.close_tag = ""
                    continue
                # Closing tag not seen yet: stream the reasoning so far, holding
                # back a possible partial close tag at the tail.
                hold = _pending_tag_len(self.buffer, (self.close_tag,))
                emit = self.buffer[: len(self.buffer) - hold]
                self.buffer = self.buffer[len(self.buffer) - hold :]
                if emit:
                    to_yield.append(self._reasoning_chunk(chunk, emit))
                break

            open_idx, open_tag = self._find_open(self.buffer)
            if open_idx != -1:
                before = self.buffer[:open_idx]
                if before:
                    to_yield.append(self._content_chunk(chunk, before))
                self.buffer = self.buffer[open_idx + len(open_tag) :]
                self.in_reasoning = True
                self.close_tag = self._close_for(open_tag)
                continue
            # No opening tag: stream content, holding back a possible partial
            # opening tag at the tail.
            hold = _pending_tag_len(self.buffer, self._open_tags)
            emit = self.buffer[: len(self.buffer) - hold]
            self.buffer = self.buffer[len(self.buffer) - hold :]
            if emit:
                to_yield.append(self._content_chunk(chunk, emit))
            break

        if not to_yield and _has_stream_signals(chunk):
            # The chunk was fully buffered/consumed but carries finish_reason or
            # usage -- forward those signals so they aren't lost.
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
            self.close_tag = ""
            return [self._standalone_chunk({"reasoning_content": text})]
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

        reasonings: list[str] = []

        def _grab(match: re.Match[str]) -> str:
            reasonings.append(match.group(2))
            return ""

        new_content = self._nonstream_re.sub(_grab, content).strip()
        if reasonings:
            message["content"] = new_content
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
        for open_candidate, close_tag in self.tag_pairs:
            if open_candidate == open_tag:
                return close_tag
        return "</think>"

    def _content_chunk(self, chunk: dict[str, Any], text: str) -> dict[str, Any]:
        """Clone *chunk* carrying *text* as the sole content delta."""
        new_chunk = _clone_chunk(chunk)
        new_delta = new_chunk["choices"][0]["delta"]
        for field in ("reasoning_content", "reasoning", "thinking"):
            new_delta.pop(field, None)
        new_delta["content"] = text
        return new_chunk

    def _reasoning_chunk(self, chunk: dict[str, Any], text: str) -> dict[str, Any]:
        """Clone *chunk* carrying *text* as the sole reasoning_content delta."""
        new_chunk = _clone_chunk(chunk)
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
