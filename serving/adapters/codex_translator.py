"""Pure-function translator between Chat Completions and Codex Responses API formats.

Converts request/response payloads bidirectionally so the gateway can accept
standard OpenAI Chat Completions requests and route them through Codex
subscription accounts transparently.

No side effects, no I/O — all functions are stateless and unit-testable.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ROLE_MAP = {
    "system": "developer",
    "user": "user",
    "assistant": "assistant",
}

_SLUG_MAP: dict[str, tuple[str, str | None]] = {
    "gpt-5.4": ("gpt-5.4", None),
    "gpt-5.3-codex": ("gpt-5.3-codex", None),
    "gpt-5.2-codex": ("gpt-5.2-codex", None),
    "gpt-5.2": ("gpt-5.2", None),
    "gpt-5.1-codex": ("gpt-5.1-codex", None),
    "gpt-5-codex": ("gpt-5-codex", None),
    "o4-mini-codex": ("o4-mini-codex", None),
}

_STOP_REASON_MAP = {
    "stop": "stop",
    "max_output_tokens": "length",
    "tool_use": "tool_calls",
}

# ---------------------------------------------------------------------------
# Request translation: Chat Completions → Codex Responses API
# ---------------------------------------------------------------------------


def translate_request(messages: list[dict[str, Any]], model: str, **params: Any) -> dict[str, Any]:
    """Convert Chat Completions messages into a Codex Responses API body.

    Args:
        messages: OpenAI Chat Completions messages list.
        model: Model ID as sent by the client (may include -low/-high suffix).
        **params: Additional request params (temperature, max_tokens, tools, etc.).

    Returns:
        Dict suitable for POST to ``/backend-api/codex/responses``.
    """
    input_items: list[dict[str, Any]] = []
    instructions = ""

    for msg in messages:
        role = msg.get("role", "")

        if role == "system":
            # System messages become the top-level `instructions` field
            # (required by the Codex Responses API).
            content = msg.get("content", "")
            if isinstance(content, str):
                instructions = (instructions + "\n" + content).strip() if instructions else content
            elif isinstance(content, list):
                texts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                joined = "\n".join(texts)
                instructions = (instructions + "\n" + joined).strip() if instructions else joined
            continue

        if role == "tool":
            # Tool result → function_call_output
            content = msg.get("content", "")
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": content if isinstance(content, str) else str(content),
                }
            )
        elif role == "assistant" and msg.get("tool_calls"):
            # Assistant message with tool calls → message + function_call items
            input_items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": _translate_content(msg.get("content"), role="assistant"),
                }
            )
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                input_items.append(
                    {
                        "type": "function_call",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", ""),
                    }
                )
        else:
            # Regular message (user/assistant without tool calls)
            input_items.append(
                {
                    "type": "message",
                    "role": _ROLE_MAP.get(role, role),
                    "content": _translate_content(msg.get("content"), role=role),
                }
            )

    slug, _slug_effort = _map_model_slug(model)
    # Explicit param takes priority; slug suffix is fallback for backward compat
    reasoning_effort = params.get("reasoning_effort") or _slug_effort

    body: dict[str, Any] = {
        "model": slug,
        "instructions": instructions,
        "input": input_items,
        "stream": True,
        "store": False,
    }

    # Codex Responses API does not support temperature at all.
    if reasoning_effort and reasoning_effort != "none":
        body["reasoning"] = {"effort": reasoning_effort}
    if params.get("tools"):
        body["tools"] = _translate_tools(params["tools"])
    if params.get("tool_choice") is not None:
        body["tool_choice"] = params["tool_choice"]

    return body


# ---------------------------------------------------------------------------
# Response translation: Codex Responses API → Chat Completions
# ---------------------------------------------------------------------------


def translate_response(codex_response: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert a Codex Responses API response into Chat Completions format.

    Args:
        codex_response: Raw JSON from ``/backend-api/codex/responses``.
        model: Model ID to include in the response.

    Returns:
        OpenAI-compatible Chat Completion response dict.
    """
    output_text = ""
    tool_calls: list[dict[str, Any]] = []

    for item in codex_response.get("output", []):
        item_type = item.get("type")
        if item_type == "message":
            for content_block in item.get("content", []):
                if content_block.get("type") == "output_text":
                    output_text += content_block.get("text", "")
        elif item_type == "function_call":
            tool_calls.append(
                {
                    "id": item.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    },
                }
            )

    message: dict[str, Any] = {"role": "assistant"}
    message["content"] = output_text if output_text else None
    if tool_calls:
        message["tool_calls"] = tool_calls

    # Map stop_reason
    stop_reason = codex_response.get("stop_reason", "stop")
    if tool_calls and stop_reason in ("tool_use", "max_output_tokens"):
        finish_reason = "tool_calls"
    else:
        finish_reason = _map_stop_reason(stop_reason)

    response_id = codex_response.get("id", uuid4().hex[:24])

    return {
        "id": f"chatcmpl-{response_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": _extract_usage(codex_response.get("usage", {})),
    }


# ---------------------------------------------------------------------------
# Streaming translation
# ---------------------------------------------------------------------------


def translate_stream_event(event_data: dict[str, Any]) -> tuple[str | None, dict | None, bool]:
    """Translate a single Codex SSE event into Chat Completions delta pieces.

    Args:
        event_data: Parsed JSON from an SSE ``data:`` line.

    Returns:
        Tuple of (content_delta, tool_call_delta, is_done).
        - content_delta: text string if this is a content delta, else None.
        - tool_call_delta: dict suitable for ``delta.tool_calls`` if this is a
          function call argument delta, else None.
        - is_done: True if the stream is complete.
    """
    event_type = event_data.get("type", "")

    if event_type == "response.output_text.delta":
        return event_data.get("delta", ""), None, False

    if event_type == "response.function_call_arguments.delta":
        tool_delta = {
            "index": event_data.get("output_index", 0),
            "id": event_data.get("item_id", ""),
            "type": "function",
            "function": {
                "name": event_data.get("name", ""),
                "arguments": event_data.get("delta", ""),
            },
        }
        return None, tool_delta, False

    if event_type == "response.completed":
        return None, None, True

    # Unrecognised event — pass through silently
    return None, None, False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Chat Completions tools to Codex Responses API format.

    Chat Completions: ``{"type": "function", "function": {"name": ..., "parameters": ...}}``
    Codex Responses:  ``{"type": "function", "name": ..., "parameters": ...}``
    """
    result = []
    for tool in tools:
        if tool.get("type") == "function" and "function" in tool:
            fn = tool["function"]
            entry: dict[str, Any] = {"type": "function", "name": fn.get("name", "")}
            if fn.get("description"):
                entry["description"] = fn["description"]
            if fn.get("parameters"):
                entry["parameters"] = fn["parameters"]
            if fn.get("strict") is not None:
                entry["strict"] = fn["strict"]
            result.append(entry)
        else:
            # Pass through unknown tool types as-is
            result.append(tool)
    return result


def _translate_content(content: Any, *, role: str = "user") -> list[dict[str, Any]]:
    """Convert message content to Responses API content blocks.

    Uses ``output_text`` for assistant messages and ``input_text`` for all
    other roles, matching the Codex Responses API schema.
    """
    text_type = "output_text" if role == "assistant" else "input_text"

    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]

    # Multimodal content blocks
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        if part_type == "text":
            blocks.append({"type": text_type, "text": part.get("text", "")})
        elif part_type == "image_url":
            url_data = part.get("image_url", {})
            url = url_data.get("url", "") if isinstance(url_data, dict) else str(url_data)
            blocks.append({"type": "input_image", "image_url": url})
    return blocks


def _map_model_slug(model_id: str) -> tuple[str, str | None]:
    """Return ``(codex_slug, reasoning_effort)`` for a model ID."""
    return _SLUG_MAP.get(model_id, (model_id, None))


def _map_stop_reason(stop_reason: str) -> str:
    """Map Codex stop_reason to Chat Completions finish_reason."""
    return _STOP_REASON_MAP.get(stop_reason, "stop")


def _extract_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Map Codex usage fields to Chat Completions usage fields."""
    prompt_tokens = int(usage.get("input_tokens", 0))
    completion_tokens = int(usage.get("output_tokens", 0))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
