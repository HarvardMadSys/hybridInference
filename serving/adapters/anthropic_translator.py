"""Anthropic Messages <-> OpenAI Chat Completions translator (reverse direction).

Forward direction (OpenAI -> Anthropic) lives in serving/adapters/claude_format.py.
This module handles the reverse direction needed when Anthropic-format requests
arrive on the northbound surface and must be dispatched to OpenAI-style backends.

Pure functions plus one stateful streaming translator. No I/O, no logging.
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# Request translation: Anthropic -> OpenAI
# ---------------------------------------------------------------------------


def anthropic_request_to_openai(
    body: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Translate an Anthropic Messages API request to OpenAI Chat Completions.

    Returns:
        (openai_messages, openai_params) where openai_params holds non-message
        fields like max_tokens, temperature, tools, etc.
    """
    messages: list[dict[str, Any]] = []

    # System prompt -> system message prepended.
    system = body.get("system")
    system_text = _flatten_system(system)
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages", []):
        messages.extend(_translate_message(msg))

    params = _translate_params(body)
    return messages, params


def _flatten_system(system: Any) -> str | None:
    if not system:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n\n".join(p for p in parts if p) or None
    return None


def _translate_message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate a single Anthropic message.

    May produce multiple OpenAI messages (tool_result blocks become separate
    role:"tool" messages).
    """
    role = msg.get("role")
    content = msg.get("content")

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": ""}]

    # Bucket blocks: text/image -> content parts, tool_use -> tool_calls,
    # tool_result -> separate role:"tool" messages.
    text_parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    text_only_buffer: list[str] = []

    for block in content:
        btype = block.get("type")

        if btype == "text":
            text_parts.append({"type": "text", "text": block.get("text", "")})
            text_only_buffer.append(block.get("text", ""))

        elif btype == "image":
            url = _image_block_to_url(block.get("source", {}))
            if url is not None:
                text_parts.append({"type": "image_url", "image_url": {"url": url}})

        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )

        elif btype == "tool_result":
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _flatten_tool_result_content(block.get("content")),
                }
            )

        # Unknown blocks ignored.

    out: list[dict[str, Any]] = []

    # Build the primary translated message (text + image parts + tool_calls).
    has_image = any(p.get("type") == "image_url" for p in text_parts)
    if text_parts or tool_calls:
        primary: dict[str, Any] = {"role": role}
        if has_image:
            primary["content"] = text_parts
        else:
            primary["content"] = "".join(text_only_buffer) if text_only_buffer else None
        if tool_calls:
            primary["tool_calls"] = tool_calls
        out.append(primary)

    # tool_result blocks become standalone tool-role messages, appended after.
    out.extend(tool_results)
    return out


def _image_block_to_url(source: dict[str, Any]) -> str | None:
    src_type = source.get("type")
    if src_type == "url":
        return source.get("url")
    if src_type == "base64":
        media_type = source.get("media_type", "application/octet-stream")
        data = source.get("data", "")
        return f"data:{media_type};base64,{data}"
    return None


def _flatten_tool_result_content(content: Any) -> str:
    """Flatten Anthropic tool_result content to a string.

    Anthropic tool_result content can be a string or list of blocks; OpenAI tool
    messages take a string. Concatenates text blocks and ignores non-text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _translate_params(body: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if "max_tokens" in body:
        params["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        params["temperature"] = body["temperature"]
    if "top_p" in body:
        params["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        params["stop"] = body["stop_sequences"]
    if body.get("stream"):
        params["stream"] = True
    if body.get("tools"):
        params["tools"] = [_translate_tool(t) for t in body["tools"]]
    if "tool_choice" in body:
        tc = _translate_tool_choice(body["tool_choice"])
        if tc is not None:
            params["tool_choice"] = tc
    metadata = body.get("metadata") or {}
    user_id = metadata.get("user_id")
    if user_id:
        params["user"] = user_id
    # thinking and other Anthropic-only top-level fields are silently dropped.
    return params


def _translate_tool_choice(tc: Any) -> Any:
    """Translate Anthropic tool_choice to OpenAI tool_choice format."""
    if not isinstance(tc, dict):
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    return None


def _translate_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


# ---------------------------------------------------------------------------
# Response translation: OpenAI -> Anthropic
# ---------------------------------------------------------------------------

_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}


def openai_response_to_anthropic(resp: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Translate an OpenAI ChatCompletion response to Anthropic Messages format."""
    choice = (resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content_blocks: list[dict[str, Any]] = []

    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            tool_input = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            tool_input = {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": tool_input,
            }
        )

    finish = choice.get("finish_reason") or "stop"
    stop_reason = _FINISH_REASON_MAP.get(finish, "end_turn")

    raw_id = resp.get("id") or ""
    msg_id = raw_id if raw_id.startswith("msg_") else f"msg_{raw_id}" if raw_id else "msg_"

    usage_in = resp.get("usage") or {}
    anthropic_usage: dict[str, int] = {
        "input_tokens": int(usage_in.get("prompt_tokens", 0)),
        "output_tokens": int(usage_in.get("completion_tokens", 0)),
    }
    cached = (usage_in.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached:
        anthropic_usage["cache_read_input_tokens"] = int(cached)

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }
