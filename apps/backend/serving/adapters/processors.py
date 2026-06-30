"""Output processors for converting model-specific formats to OpenAI standard.

This module implements the Strategy pattern to handle different model output formats.
It separates the complex parsing logic from the network adapters.
"""

from __future__ import annotations

import json
import re
import time
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
                # Emit a chunk with tool_calls
                # IMPORTANT: Set finish_reason to "tool_calls" to override upstream "stop"
                chunk = {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": tool_calls, "content": None},
                            "finish_reason": "tool_calls",
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
        return to_yield

    def _parse_glm_tool_xml(self, xml_text: str) -> list[dict[str, Any]] | None:
        """Parse GLM tool XML into OpenAI tool_calls format."""
        # Try to find tool name
        # Format varies: <tool_call>NAME</tool_call>... or <tool_call>NAME\n<arg_key>...

        # Heuristic 1: Extract tool name
        # Look for text after <tool_call> and before next tag or newline
        name_match = re.search(r"<tool_call>\s*([^<\n]+)", xml_text)
        if not name_match:
            return None
        name = name_match.group(1).strip()

        # Heuristic 2: Extract args
        # GLM usually emits <arg_key>K</arg_key><arg_value>V</arg_value>
        args = {}
        keys = re.findall(r"<arg_key>(.*?)</arg_key>", xml_text, re.DOTALL)
        values = re.findall(r"<arg_value>(.*?)</arg_value>", xml_text, re.DOTALL)

        for k, v in zip(keys, values, strict=False):
            k = k.strip()
            v = v.strip()
            # Value might be a JSON string (e.g. ["ls", "-la"]) or raw string
            try:
                # If it looks like JSON, try to parse it to clean it up, then dump back?
                # Actually, OpenAI expects 'arguments' to be a JSON string of the *whole* object.
                # GLM gives us separate keys.
                # We construct the dict then dump it.

                # GLM sometimes puts JSON in the value: <arg_value>["bash", "-lc", ...]</arg_value>
                if (v.startswith("[") and v.endswith("]")) or (
                    v.startswith("{") and v.endswith("}")
                ):
                    args[k] = json.loads(v)
                else:
                    args[k] = v
            except Exception:
                args[k] = v

        # Construct OpenAI tool call
        return [
            {
                "index": 0,
                "id": f"call_glm_{int(time.time())}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ]

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

    def process_stream_chunk(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Process a streaming chunk from Qwen3-Coder models.

        Buffers content when tool call XML is detected. Emits regular text
        immediately (with partial-tag safety). Passes through native tool_calls
        untouched.
        """
        choices = chunk.get("choices", [])
        if not choices:
            return [chunk]

        delta = choices[0].get("delta", {})

        # Pass through native tool_calls untouched (but ignore null/empty)
        if delta.get("tool_calls"):
            return [chunk]

        content = delta.get("content")
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
                chunk = {
                    "id": f"chatcmpl-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": tool_calls, "content": None},
                            "finish_reason": "tool_calls",
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
                    "id": f"call_qwen_{int(time.time())}_{i}",
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
            if chunk.get("usage"):
                return [chunk]
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


def _clone_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """Deep copy structure of a chunk for modification."""
    new_chunk = chunk.copy()
    if "choices" in chunk:
        new_chunk["choices"] = [c.copy() for c in chunk["choices"]]
        for _i, c in enumerate(new_chunk["choices"]):
            if "delta" in c:
                c["delta"] = c["delta"].copy()
    return new_chunk


_PROCESSOR_MAP: dict[str, type[BaseProcessor]] = {
    "default": DefaultProcessor,
    "glm": GLMProcessor,
    "qwen_coder": QwenCoderProcessor,
    "think_block": ThinkBlockProcessor,
}


def get_processor(model_id: str | None, override: str | None = None) -> BaseProcessor:
    """Factory function to get the appropriate processor.

    Args:
        model_id: Model identifier for auto-detection.
        override: Explicit processor name (bypasses auto-detection).
                  Values: "default", "glm", "qwen_coder", "think_block".
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
