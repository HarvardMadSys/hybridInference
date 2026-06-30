"""OpenAI chat stream serialization for the public /v1/chat/completions surface.

Provides serializer modes and chunk sanitization so completions.py can maintain
a unified API contract: default passthrough (reasoning_content visible),
opt-in strict OpenAI via X-Reasoning-Passthrough: false header.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class SerializerMode(str, Enum):
    """Public chat completions serializer modes."""

    STRICT_OPENAI = "strict_openai"
    REASONING_PASSTHROUGH = "reasoning_passthrough"


@dataclass
class SanitizeResult:
    """Result of sanitizing a chat completion chunk."""

    chunk_json: dict | None
    should_forward: bool
    usage_data: dict | None
    routing_info: dict | None


@dataclass
class SanitizeResponseResult:
    """Result of sanitizing a non-streaming chat completion response."""

    response_json: dict
    routing_info: dict | None


def resolve_mode(headers: Mapping[str, str]) -> SerializerMode:
    """Resolve serializer mode from request headers.

    Default is reasoning passthrough. Opt in to strict OpenAI by sending
    ``X-Reasoning-Passthrough`` set to any of ``false``, ``0``, or ``no``
    (case-insensitive, surrounding whitespace ignored). Any other value
    — including missing, empty, ``true``, or unrecognized strings — leaves
    the response in passthrough mode.
    """
    passthrough_raw = headers.get("x-reasoning-passthrough", "").strip().lower()
    if passthrough_raw in ("false", "0", "no"):
        return SerializerMode.STRICT_OPENAI
    return SerializerMode.REASONING_PASSTHROUGH


def sanitize_chunk(chunk_json: dict, mode: SerializerMode) -> SanitizeResult:
    """Sanitize a parsed chunk for the public API.

    - Always strips _routing (internal metadata, never sent to client).
    - Drops chunks that were pure routing-metadata (no choices, no usage)
      so the synthetic ``_routing`` chunk emitted by FixedRouter never
      reaches the client.
    - In strict mode: drops reasoning-only chunks, removes reasoning fields
      from mixed chunks.
    - In passthrough mode: preserves reasoning fields as-is.

    Returns metadata so completions.py can keep usage/routing/tool-call
    accumulation logic working.
    """
    usage_data = chunk_json.get("usage")
    routing_info = chunk_json.pop("_routing", None)

    # Suppress synthetic routing-only chunks (no choices, no usage). These are
    # emitted by FixedRouter.stream_chat_completion and RouteWiseRouter so
    # completions.py can recover the upstream provider/pricing for DB logging,
    # but they carry no client-visible payload.
    if routing_info is not None and not chunk_json.get("choices") and usage_data is None:
        return SanitizeResult(
            chunk_json=None,
            should_forward=False,
            usage_data=usage_data,
            routing_info=routing_info,
        )

    if mode == SerializerMode.REASONING_PASSTHROUGH:
        return SanitizeResult(
            chunk_json=chunk_json,
            should_forward=True,
            usage_data=usage_data,
            routing_info=routing_info,
        )

    # Strict mode: drop reasoning-only chunks, strip reasoning from mixed
    choices = chunk_json.get("choices", [])
    if not choices:
        return SanitizeResult(
            chunk_json=chunk_json,
            should_forward=True,
            usage_data=usage_data,
            routing_info=routing_info,
        )

    delta = choices[0].get("delta", {})
    has_reasoning = (
        bool(delta.get("reasoning_content"))
        or bool(delta.get("reasoning"))
        or bool(delta.get("thinking"))
    )
    has_content = bool(delta.get("content"))
    has_tool_calls = bool(delta.get("tool_calls"))

    if has_reasoning and not has_content and not has_tool_calls:
        return SanitizeResult(
            chunk_json=None,
            should_forward=False,
            usage_data=usage_data,
            routing_info=routing_info,
        )

    if has_reasoning and (has_content or has_tool_calls):
        delta.pop("reasoning_content", None)
        delta.pop("reasoning", None)
        delta.pop("thinking", None)

    return SanitizeResult(
        chunk_json=chunk_json,
        should_forward=True,
        usage_data=usage_data,
        routing_info=routing_info,
    )


def sanitize_response(response_json: dict, mode: SerializerMode) -> SanitizeResponseResult:
    """Sanitize a non-streaming chat completion response for the public API.

    - Always strips `_routing`.
    - In strict mode: removes message reasoning fields.
    - In passthrough mode: preserves message reasoning fields.
    """
    sanitized = dict(response_json)
    routing_info = sanitized.pop("_routing", None)

    if mode == SerializerMode.REASONING_PASSTHROUGH:
        return SanitizeResponseResult(response_json=sanitized, routing_info=routing_info)

    choices = sanitized.get("choices", [])
    if not choices:
        return SanitizeResponseResult(response_json=sanitized, routing_info=routing_info)

    choice = dict(choices[0])
    message = dict(choice.get("message", {}))
    if any(key in message for key in ("reasoning_content", "reasoning", "thinking")):
        message.pop("reasoning_content", None)
        message.pop("reasoning", None)
        message.pop("thinking", None)
        new_choices = list(choices)
        choice["message"] = message
        new_choices[0] = choice
        sanitized["choices"] = new_choices

    return SanitizeResponseResult(response_json=sanitized, routing_info=routing_info)
