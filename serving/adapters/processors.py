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

        # Check if we should enter tool mode
        # We look for <tool_call> or <tool> depending on model variant
        if ("<tool_call>" in self.buffer or "<tool>" in self.buffer) and not self.in_tool_mode:
            self.in_tool_mode = True

        to_yield = []

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

        for _i, k in enumerate(keys, strict=False):
            k = k.strip()
            v = values[_i]
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


def _clone_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """Deep copy structure of a chunk for modification."""
    new_chunk = chunk.copy()
    if "choices" in chunk:
        new_chunk["choices"] = [c.copy() for c in chunk["choices"]]
        for _i, c in enumerate(new_chunk["choices"]):
            if "delta" in c:
                c["delta"] = c["delta"].copy()
    return new_chunk


def get_processor(model_id: str | None) -> BaseProcessor:
    """Factory function to get the appropriate processor."""
    if not model_id:
        return DefaultProcessor()

    model_id_lower = model_id.lower()

    # Auto-detect GLM models
    if "glm-4" in model_id_lower or "glm4" in model_id_lower:
        return GLMProcessor()

    return DefaultProcessor()
