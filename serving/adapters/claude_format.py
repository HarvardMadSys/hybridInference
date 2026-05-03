"""Shared format translation between OpenAI Chat Completions and Anthropic Messages API.

Extracted from ClaudeAdapter (claude.py) so that both the Vertex AI adapter
and the Claude subscription adapter can share identical translation logic.

Three categories:
  - Request:   OpenAI messages/tools → Claude messages/tools
  - Response:  Claude response → OpenAI-compatible dict
  - Streaming: Claude SSE events → OpenAI-compatible deltas
"""

from __future__ import annotations

import base64
import json
from contextlib import suppress
from typing import Any

from .base import UsageInfo

# ---------------------------------------------------------------------------
# Image constants
# ---------------------------------------------------------------------------

SUPPORTED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}

MIME_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}

EXTENSION_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def convert_content_block(block: dict[str, Any] | str) -> dict[str, Any]:
    """Convert a single content block from OpenAI format to Claude format."""
    if isinstance(block, str):
        return {"type": "text", "text": block}

    if not isinstance(block, dict):
        return {"type": "text", "text": str(block)}

    block_type = block.get("type")

    if block_type == "text":
        return {"type": "text", "text": block.get("text", "")}

    if block_type == "image_url":
        image_url_obj = block.get("image_url", {})
        url = image_url_obj if isinstance(image_url_obj, str) else image_url_obj.get("url", "")

        if not url:
            return {"type": "text", "text": "[Invalid image: no URL provided]"}

        if url.startswith("data:"):
            return parse_data_url(url)
        else:
            return {
                "type": "image",
                "source": {"type": "url", "url": url},
            }

    if "text" in block:
        return {"type": "text", "text": block["text"]}

    with suppress(Exception):
        return {"type": "text", "text": json.dumps(block)}
    return {"type": "text", "text": str(block)}


def parse_data_url(data_url: str) -> dict[str, Any]:
    """Parse a data URL and convert to Claude image format.

    RFC 2397: data:[<mediatype>][;base64],<data>
    """
    comma_idx = data_url.find(",")
    if comma_idx == -1 or not data_url.startswith("data:"):
        return {"type": "text", "text": "[Invalid data URL format]"}

    metadata = data_url[5:comma_idx]
    raw_data = data_url[comma_idx + 1 :]

    is_base64 = ";base64" in metadata.lower()
    media_type = metadata.split(";")[0].strip() if ";" in metadata else metadata.strip()

    if not media_type:
        media_type = "image/jpeg"

    normalized_media_type = media_type.lower().strip()
    if normalized_media_type in MIME_TYPE_ALIASES:
        media_type = MIME_TYPE_ALIASES[normalized_media_type]
    elif normalized_media_type in SUPPORTED_IMAGE_TYPES:
        media_type = normalized_media_type
    else:
        return {"type": "text", "text": f"[Unsupported image type: {media_type}]"}

    if is_base64:
        base64_data = raw_data
    else:
        try:
            from urllib.parse import unquote_to_bytes

            decoded_bytes = unquote_to_bytes(raw_data)
            base64_data = base64.b64encode(decoded_bytes).decode("ascii")
        except Exception:
            return {"type": "text", "text": "[Failed to decode non-base64 data URL]"}

    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64_data},
    }


def infer_media_type_from_url(url: str) -> str:
    """Infer media type from URL extension, defaults to image/jpeg."""
    path = url.split("?")[0].lower()
    for ext, mime in EXTENSION_TO_MIME.items():
        if path.endswith(ext):
            return mime
    return "image/jpeg"


def convert_content_blocks(content: str | list[Any] | Any) -> list[dict[str, Any]]:
    """Convert content from OpenAI format to Claude content block list."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    if isinstance(content, list):
        return [convert_content_block(item) for item in content]

    if content is None:
        return []

    with suppress(Exception):
        return [{"type": "text", "text": json.dumps(content)}]
    return [{"type": "text", "text": str(content)}]


def extract_system(messages: list[dict[str, Any]]) -> str | None:
    """Extract system text from input messages if present."""
    parts: list[str] = []
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    with suppress(Exception):
                        parts.append(json.dumps(block))
        elif content is not None:
            with suppress(Exception):
                parts.append(json.dumps(content))
    joined = "\n\n".join([p for p in parts if p])
    return joined if joined else None


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI tool format to Claude tool format."""
    claude_tools = []
    for tool in tools:
        if tool.get("type") == "function":
            func = tool.get("function", {})
            if func.get("name"):
                claude_tools.append(
                    {
                        "name": func.get("name"),
                        "description": func.get("description", ""),
                        "input_schema": func.get("parameters", {}),
                    }
                )
    return claude_tools


def convert_tool_choice(tool_choice: str | dict[str, Any], payload: dict[str, Any]) -> None:
    """Convert OpenAI tool_choice to Claude format, mutating payload in place.

    May remove ``payload["tools"]`` if tool_choice is ``"none"``.
    """
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            pass  # default
        elif tool_choice in ("required", "any"):
            payload["tool_choice"] = {"type": "any"}
        elif tool_choice == "none":
            payload.pop("tools", None)
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        func_name = tool_choice.get("function", {}).get("name")
        if func_name:
            payload["tool_choice"] = {"type": "tool", "name": func_name}


def convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI-format messages to Claude format.

    Handles:
    - System message filtering (extracted separately)
    - Alternating user/assistant roles
    - Tool_use / tool_result ordering
    - Content block conversion (text, images, tool results)
    """
    # Phase 1: Convert all messages to Claude format
    raw_converted: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            continue

        if role == "user":
            content_blocks = convert_content_blocks(content)
            raw_converted.append({"role": "user", "content": content_blocks, "_type": "user"})

        elif role == "assistant":
            text_blocks: list[dict[str, Any]] = []
            tool_use_blocks: list[dict[str, Any]] = []

            if isinstance(content, str) and content.strip():
                text_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for b in content:
                    text_blocks.append(convert_content_block(b))
            elif content:
                with suppress(Exception):
                    text_blocks.append({"type": "text", "text": json.dumps(content)})

            tool_calls = msg.get("tool_calls")
            tool_use_ids: list[str] = []
            if isinstance(tool_calls, list) and tool_calls:
                for tc in tool_calls:
                    func = (tc or {}).get("function", {})
                    name = func.get("name")
                    args_raw = func.get("arguments") or "{}"
                    try:
                        args_obj = json.loads(args_raw)
                    except Exception:
                        args_obj = {}
                    tool_id = tc.get("id")
                    tool_use_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": name,
                            "input": args_obj,
                        }
                    )
                    if tool_id:
                        tool_use_ids.append(tool_id)

            blocks = text_blocks + tool_use_blocks
            if blocks:
                raw_converted.append(
                    {
                        "role": "assistant",
                        "content": blocks,
                        "_type": "assistant",
                        "_tool_use_ids": tool_use_ids,
                    }
                )

        elif role == "tool":
            tool_use_id = msg.get("tool_call_id") or msg.get("id")
            tool_result_block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "is_error": False,
            }
            if isinstance(content, str):
                tool_result_block["content"] = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                tool_result_block["content"] = [convert_content_block(item) for item in content]
            else:
                tool_result_block["content"] = [{"type": "text", "text": json.dumps(content or "")}]

            raw_converted.append(
                {
                    "role": "user",
                    "content": [tool_result_block],
                    "_type": "tool_result",
                    "_tool_use_id": tool_use_id,
                }
            )

    # Phase 2: Build tool_result map for reordering
    tool_result_map: dict[str, dict[str, Any]] = {}
    for msg in raw_converted:
        if msg.get("_type") == "tool_result":
            tool_use_id = msg.get("_tool_use_id")
            if tool_use_id:
                tool_result_map[tool_use_id] = msg

    # Phase 3: Build final message list with correct ordering
    converted: list[dict[str, Any]] = []
    pending_tool_use_ids: list[str] = []
    used_tool_result_ids: set[str] = set()

    for msg in raw_converted:
        msg_type = msg.get("_type")
        role = msg["role"]
        content = msg["content"]

        if msg_type == "tool_result":
            tool_use_id = msg.get("_tool_use_id")
            if tool_use_id in used_tool_result_ids:
                continue
            if converted and converted[-1]["role"] == "user":
                converted[-1]["content"].extend(content)
            else:
                converted.append({"role": "user", "content": list(content)})
            used_tool_result_ids.add(tool_use_id)
            continue

        if role == "assistant":
            tool_use_ids = msg.get("_tool_use_ids", [])

            if converted and converted[-1]["role"] == "assistant":
                existing_content = converted[-1]["content"]
                existing_text = [b for b in existing_content if b.get("type") != "tool_use"]
                existing_tools = [b for b in existing_content if b.get("type") == "tool_use"]
                new_text = [b for b in content if b.get("type") != "tool_use"]
                new_tools = [b for b in content if b.get("type") == "tool_use"]
                converted[-1]["content"] = existing_text + new_text + existing_tools + new_tools
                pending_tool_use_ids.extend(tool_use_ids)
            else:
                if pending_tool_use_ids:
                    tool_result_blocks = []
                    for tid in pending_tool_use_ids:
                        if tid in tool_result_map and tid not in used_tool_result_ids:
                            result_msg = tool_result_map[tid]
                            tool_result_blocks.extend(result_msg["content"])
                            used_tool_result_ids.add(tid)
                    if tool_result_blocks:
                        if converted and converted[-1]["role"] == "user":
                            converted[-1]["content"].extend(tool_result_blocks)
                        else:
                            converted.append({"role": "user", "content": tool_result_blocks})
                    pending_tool_use_ids.clear()

                converted.append({"role": "assistant", "content": list(content)})
                pending_tool_use_ids.extend(tool_use_ids)

        elif role == "user":
            if pending_tool_use_ids:
                tool_result_blocks = []
                for tid in pending_tool_use_ids:
                    if tid in tool_result_map and tid not in used_tool_result_ids:
                        result_msg = tool_result_map[tid]
                        tool_result_blocks.extend(result_msg["content"])
                        used_tool_result_ids.add(tid)
                if tool_result_blocks:
                    if converted and converted[-1]["role"] == "user":
                        converted[-1]["content"].extend(tool_result_blocks)
                        converted[-1]["content"].extend(content)
                    else:
                        converted.append(
                            {"role": "user", "content": tool_result_blocks + list(content)}
                        )
                    pending_tool_use_ids.clear()
                else:
                    pending_tool_use_ids.clear()
                    if converted and converted[-1]["role"] == "user":
                        converted[-1]["content"].extend(content)
                    else:
                        converted.append({"role": "user", "content": list(content)})
            else:
                if converted and converted[-1]["role"] == "user":
                    converted[-1]["content"].extend(content)
                else:
                    converted.append({"role": "user", "content": list(content)})

    # Handle remaining pending tool_results
    if pending_tool_use_ids:
        tool_result_blocks = []
        for tid in pending_tool_use_ids:
            if tid in tool_result_map and tid not in used_tool_result_ids:
                result_msg = tool_result_map[tid]
                tool_result_blocks.extend(result_msg["content"])
                used_tool_result_ids.add(tid)
        if tool_result_blocks:
            if converted and converted[-1]["role"] == "user":
                converted[-1]["content"].extend(tool_result_blocks)
            else:
                converted.append({"role": "user", "content": tool_result_blocks})

    return converted


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_response_content(
    content_blocks: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]] | None]:
    """Parse Claude content blocks into text and OpenAI-format tool_calls.

    Returns:
        (text_content, tool_calls_or_none)
    """
    text = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_index = 0

    for block in content_blocks:
        if block.get("type") == "text":
            text += block.get("text", "")
        elif block.get("type") == "tool_use":
            if tool_calls is None:
                tool_calls = []
            tool_calls.append(
                {
                    "index": tool_index,
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )
            tool_index += 1

    return text, tool_calls


def parse_usage(usage_data: dict[str, Any]) -> UsageInfo:
    """Parse Claude usage dict into UsageInfo with cache token separation."""
    input_tokens = int(usage_data.get("input_tokens", 0) or 0)
    cache_read = int(usage_data.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage_data.get("cache_creation_input_tokens", 0) or 0)
    output_tokens = int(usage_data.get("output_tokens", 0) or 0)
    thinking_tokens = int(usage_data.get("thinking_tokens", 0) or 0)

    # Anthropic's input_tokens, cache_read_input_tokens, and cache_creation_input_tokens
    # are disjoint. Use the cache-inclusive (OpenAI-style) total here so storage and
    # cost calculation see a consistent prompt_tokens semantic across providers.
    prompt_tokens = input_tokens + cache_read + cache_write
    return UsageInfo(
        prompt_tokens=prompt_tokens,
        completion_tokens=output_tokens,
        total_tokens=prompt_tokens + output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=thinking_tokens,
    )


def map_stop_reason(claude_stop_reason: str) -> str:
    """Map Claude stop_reason to OpenAI finish_reason."""
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }
    return mapping.get(claude_stop_reason, "stop")


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


class ToolCallAccumulator:
    """State machine for accumulating streaming tool_use blocks.

    Usage::

        acc = ToolCallAccumulator()
        # on content_block_start with type=tool_use:
        acc.start_tool(tool_id, tool_name)
        # on content_block_delta with type=input_json_delta:
        acc.accumulate_json(partial_json)
        # on content_block_stop:
        acc.finish_tool()
        # on message_stop or when done:
        tool_calls = acc.get_completed()
    """

    def __init__(self) -> None:
        self._current_tool: dict[str, Any] | None = None
        self._input_buffer: str = ""
        self._current_index: int = 0
        self._completed: list[dict[str, Any]] = []

    def start_tool(self, tool_id: str, tool_name: str) -> None:
        """Begin accumulating a new tool_use block."""
        self._current_tool = {
            "index": self._current_index,
            "id": tool_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": ""},
        }
        self._input_buffer = ""

    def accumulate_json(self, partial_json: str) -> None:
        """Append a partial JSON fragment to the current tool's arguments."""
        self._input_buffer += partial_json

    def finish_tool(self) -> None:
        """Finalize the current tool_use block and move to completed list."""
        if self._current_tool is not None:
            self._current_tool["function"]["arguments"] = self._input_buffer
            self._completed.append(self._current_tool)
            self._current_tool = None
            self._input_buffer = ""
            self._current_index += 1

    @property
    def has_active_tool(self) -> bool:
        """Return True if a tool call is currently being accumulated."""
        return self._current_tool is not None

    def get_completed(self) -> list[dict[str, Any]]:
        """Return all completed tool calls (may be empty)."""
        return self._completed

    def reset(self) -> None:
        """Clear all accumulated state."""
        self._current_tool = None
        self._input_buffer = ""
        self._current_index = 0
        self._completed = []


def handle_stream_event(
    event: dict[str, Any],
    accumulator: ToolCallAccumulator,
) -> StreamEventResult:
    """Dispatch a single Claude SSE event and return typed result.

    Returns a StreamEventResult with the relevant delta populated.
    """
    event_type = event.get("type")

    if event_type == "message_start":
        message = event.get("message", {})
        usage_data = message.get("usage", {})
        input_tokens = int(usage_data.get("input_tokens", 0) or 0)
        cache_read = int(usage_data.get("cache_read_input_tokens", 0) or 0)
        cache_write = int(usage_data.get("cache_creation_input_tokens", 0) or 0)
        return StreamEventResult(
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    if event_type == "content_block_start":
        block = event.get("content_block", {})
        if block.get("type") == "tool_use":
            accumulator.start_tool(
                tool_id=block.get("id", ""),
                tool_name=block.get("name", ""),
            )
        return StreamEventResult()

    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type")

        if delta_type == "text_delta":
            return StreamEventResult(text_delta=delta.get("text", ""))

        if delta_type == "input_json_delta":
            accumulator.accumulate_json(delta.get("partial_json", ""))
            return StreamEventResult()

        return StreamEventResult()

    if event_type == "content_block_stop":
        if accumulator.has_active_tool:
            accumulator.finish_tool()
        return StreamEventResult()

    if event_type == "message_delta":
        usage_delta = event.get("usage", {})
        output_tokens = int(usage_delta.get("output_tokens", 0) or 0)
        stop_reason = event.get("delta", {}).get("stop_reason")
        finish_reason = map_stop_reason(stop_reason) if stop_reason else None
        return StreamEventResult(output_tokens=output_tokens, finish_reason=finish_reason)

    if event_type == "message_stop":
        return StreamEventResult(is_done=True)

    return StreamEventResult()


class StreamEventResult:
    """Result from processing a single Claude SSE event."""

    __slots__ = (
        "cache_read_tokens",
        "cache_write_tokens",
        "finish_reason",
        "input_tokens",
        "is_done",
        "output_tokens",
        "text_delta",
    )

    def __init__(
        self,
        *,
        text_delta: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        finish_reason: str | None = None,
        is_done: bool = False,
    ) -> None:
        self.text_delta = text_delta
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_write_tokens = cache_write_tokens
        self.finish_reason = finish_reason
        self.is_done = is_done


def build_final_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> dict[str, Any]:
    """Build final usage dict with cache token separation for streaming."""
    # Anthropic input/cache_read/cache_creation are disjoint; report
    # prompt_tokens cache-inclusive (OpenAI semantic) for consistent storage
    # and downstream cost calculation.
    prompt_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
    usage: dict[str, Any] = {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(output_tokens),
        "total_tokens": int(prompt_tokens + output_tokens),
    }
    if cache_read_input_tokens > 0:
        usage["cache_read_tokens"] = int(cache_read_input_tokens)
    if cache_creation_input_tokens > 0:
        usage["cache_write_tokens"] = int(cache_creation_input_tokens)
    return usage
