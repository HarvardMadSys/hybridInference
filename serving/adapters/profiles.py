"""Minimal provider profiles for OpenAICompatAdapter usage extraction.

Profiles define how to normalize upstream usage data into the public UsageInfo
format. Names are chosen to grow into a fuller framework later.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from .base import UsageInfo


class ProviderProfile(str, Enum):
    """Provider profile identifier for usage extraction strategy."""

    AZURE_OPENAI = "azure_openai"
    DEFAULT = "default"
    DEEPSEEK = "deepseek"
    ZHIPU = "zhipu"


def get_usage_normalizer(profile: ProviderProfile) -> Callable[[dict[str, Any]], UsageInfo]:
    """Return the usage normalizer for the given profile."""
    if profile == ProviderProfile.AZURE_OPENAI:
        return normalize_usage_azure_openai
    if profile == ProviderProfile.DEEPSEEK:
        return normalize_usage_deepseek
    return normalize_usage_default


def transform_payload_for_profile(
    profile: ProviderProfile, payload: dict[str, Any], *, stream: bool
) -> dict[str, Any]:
    """Apply provider-specific payload transforms."""
    if profile != ProviderProfile.AZURE_OPENAI:
        return payload

    transformed = dict(payload)
    transformed.pop("model", None)

    if "max_tokens" in transformed:
        transformed["max_completion_tokens"] = transformed.pop("max_tokens")

    for field in (
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "top_k",
        "min_p",
        "stop",
    ):
        transformed.pop(field, None)

    if stream:
        transformed["stream_options"] = {"include_usage": True}

    return transformed


def filter_response_format(
    profile: ProviderProfile, response_format: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Return the response_format dict to forward upstream, or None to omit.

    Profile-specific filtering preserves compatibility with providers that
    support only a subset of structured-output options.
    """
    if not response_format:
        return None
    if profile == ProviderProfile.DEEPSEEK:
        if response_format.get("type") == "json_object":
            return {"type": "json_object"}
        return None
    return response_format


def supports_guided_json(profile: ProviderProfile) -> bool:
    """Whether the provider supports the vLLM-style guided_json extension."""
    return profile not in (ProviderProfile.AZURE_OPENAI, ProviderProfile.DEEPSEEK)


def default_chat_path(profile: ProviderProfile) -> str | None:
    """Return the provider's non-standard chat path, if any."""
    if profile in (
        ProviderProfile.AZURE_OPENAI,
        ProviderProfile.ZHIPU,
    ):
        return "/chat/completions"
    return None


def normalize_messages_for_profile(
    profile: ProviderProfile, messages: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Return provider-specific normalized messages, or None for default behavior."""
    if profile == ProviderProfile.AZURE_OPENAI:
        return _normalize_azure_openai_messages(messages)
    return None


def normalize_tools_for_profile(
    profile: ProviderProfile, tools: list[dict[str, Any]] | None
) -> list[dict[str, Any]] | None:
    """Return provider-specific normalized tool definitions."""
    return tools


def resolve_tool_choice_for_profile(profile: ProviderProfile, tool_choice: Any) -> Any:
    """Resolve provider-specific tool choice defaults."""
    if tool_choice is not None:
        return tool_choice
    return None


def get_stream_idle_timeout_seconds(profile: ProviderProfile) -> float | None:
    """Return a provider-specific stream idle timeout in seconds, if any."""
    return None


def extract_tool_calls_for_profile(
    profile: ProviderProfile, message: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Extract or normalize tool calls from a response message."""
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return tool_calls
    return None


def function_call_delta_to_tool_calls(
    profile: ProviderProfile, function_call: dict[str, Any] | None
) -> list[dict[str, Any]] | None:
    """Convert a streaming function_call delta to tool_calls when needed."""
    return None


def normalize_usage_default(usage_data: dict[str, Any]) -> UsageInfo:
    """Standard OpenAI-compatible usage extraction."""
    from .base import UsageInfo

    return UsageInfo(
        prompt_tokens=usage_data.get("prompt_tokens", 0),
        completion_tokens=usage_data.get("completion_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
        reasoning_tokens=usage_data.get("reasoning_tokens", 0),
        cache_read_tokens=usage_data.get("cache_read_input_tokens", 0)
        or usage_data.get("cache_read_tokens", 0),
        cache_write_tokens=usage_data.get("cache_creation_input_tokens", 0)
        or usage_data.get("cache_write_tokens", 0),
    )


def normalize_usage_azure_openai(usage_data: dict[str, Any]) -> UsageInfo:
    """Azure OpenAI usage extraction with nested reasoning/cache fields."""
    from .base import UsageInfo

    completion_details = usage_data.get("completion_tokens_details", {})
    prompt_details = usage_data.get("prompt_tokens_details", {})

    reasoning_tokens = int(completion_details.get("reasoning_tokens", 0) or 0)
    cached_tokens = int(prompt_details.get("cached_tokens", 0) or 0)
    prompt_tokens_total = int(usage_data.get("prompt_tokens", 0) or 0)

    return UsageInfo(
        prompt_tokens=max(0, prompt_tokens_total - cached_tokens),
        completion_tokens=int(usage_data.get("completion_tokens", 0) or 0),
        total_tokens=int(usage_data.get("total_tokens", 0) or 0),
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cached_tokens,
    )


def normalize_usage_deepseek(usage_data: dict[str, Any]) -> UsageInfo:
    """DeepSeek-specific usage extraction: prompt_cache_hit/miss -> cache_read_tokens, prompt_tokens.

    DeepSeek returns prompt_cache_hit_tokens and prompt_cache_miss_tokens.
    For cost calculation:
    - cache_read_tokens = prompt_cache_hit_tokens
    - prompt_tokens = prompt_cache_miss_tokens (only non-cached tokens charged at full price)
    """
    from .base import UsageInfo

    cache_hit = usage_data.get("prompt_cache_hit_tokens", 0)
    cache_miss = usage_data.get("prompt_cache_miss_tokens", 0)
    prompt_tokens_raw = usage_data.get("prompt_tokens", 0)

    if cache_hit > 0 or cache_miss > 0:
        actual_prompt_tokens = cache_miss
        actual_cache_read = cache_hit
    else:
        actual_prompt_tokens = prompt_tokens_raw
        actual_cache_read = 0

    return UsageInfo(
        prompt_tokens=actual_prompt_tokens,
        completion_tokens=usage_data.get("completion_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
        reasoning_tokens=usage_data.get("reasoning_tokens", 0),
        cache_read_tokens=actual_cache_read,
        cache_write_tokens=usage_data.get("cache_creation_input_tokens", 0)
        or usage_data.get("cache_write_tokens", 0),
    )


def _normalize_azure_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge assistant preambles and reorder tool messages for Azure OpenAI."""
    if not messages or len(messages) < 2:
        return messages

    tool_message_map: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") == "tool":
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                tool_message_map[tool_call_id] = message

    result: list[dict[str, Any]] = []
    used_tool_ids: set[str] = set()
    index = 0

    while index < len(messages):
        message = messages[index]
        role = message.get("role")

        if role == "tool":
            index += 1
            continue

        if role == "assistant" and message.get("tool_calls"):
            merged_message = dict(message)
            cursor = index + 1
            while cursor < len(messages):
                next_message = messages[cursor]
                next_role = next_message.get("role")
                if next_role != "assistant" or next_message.get("tool_calls"):
                    break
                merged_message["content"] = _merge_assistant_content(
                    merged_message.get("content"), next_message.get("content")
                )
                cursor += 1

            result.append(merged_message)
            for tool_call in merged_message.get("tool_calls", []):
                tool_call_id = tool_call.get("id")
                if (
                    tool_call_id
                    and tool_call_id in tool_message_map
                    and tool_call_id not in used_tool_ids
                ):
                    result.append(tool_message_map[tool_call_id])
                    used_tool_ids.add(tool_call_id)

            index = cursor
            continue

        result.append(message)
        index += 1

    for tool_call_id, tool_message in tool_message_map.items():
        if tool_call_id not in used_tool_ids:
            result.append(tool_message)

    return result


def _merge_assistant_content(existing: Any | None, preamble: Any | None) -> Any | None:
    """Merge consecutive assistant content blocks conservatively."""
    if preamble is None:
        return existing
    if isinstance(preamble, str) and not preamble.strip():
        return existing
    if isinstance(preamble, list) and not preamble:
        return existing

    if existing is None:
        return preamble
    if isinstance(existing, str) and not existing.strip():
        existing = ""
    if isinstance(existing, list) and not existing:
        existing = []

    if isinstance(existing, str) and isinstance(preamble, str):
        if existing and preamble:
            return f"{existing}\n{preamble}"
        return preamble or existing

    if isinstance(existing, list) and isinstance(preamble, list):
        return [*existing, *preamble]

    if isinstance(existing, list) and isinstance(preamble, str):
        return [*existing, {"type": "text", "text": preamble}]

    if isinstance(existing, str) and isinstance(preamble, list):
        if not existing:
            return preamble
        return [{"type": "text", "text": existing}, *preamble]

    try:
        existing_str = json.dumps(existing, ensure_ascii=False)
    except TypeError:
        existing_str = str(existing)

    try:
        preamble_str = json.dumps(preamble, ensure_ascii=False)
    except TypeError:
        preamble_str = str(preamble)

    if existing_str and preamble_str:
        return f"{existing_str}\n{preamble_str}"
    return preamble_str or existing_str
