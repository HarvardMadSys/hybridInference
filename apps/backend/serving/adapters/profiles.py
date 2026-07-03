"""Minimal provider profiles for OpenAICompatAdapter usage extraction.

Profiles define how to normalize upstream usage data into the public UsageInfo
format. Names are chosen to grow into a fuller framework later.
"""

from __future__ import annotations

import math
import os
from enum import Enum
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger
from serving.utils.token_utils import extract_cache_tokens, extract_reasoning_tokens

if TYPE_CHECKING:
    from collections.abc import Callable

    from .base import UsageInfo

logger = get_logger(__name__)


class ProviderProfile(str, Enum):
    """Provider profile identifier for usage extraction strategy."""

    DEFAULT = "default"
    DEEPSEEK = "deepseek"
    KIMI = "kimi"
    MINIMAX = "minimax"
    OPENROUTER = "openrouter"
    ZAI = "zai"


def get_usage_normalizer(profile: ProviderProfile) -> Callable[[dict[str, Any]], UsageInfo]:
    """Return the usage normalizer for the given profile."""
    if profile == ProviderProfile.DEEPSEEK:
        return normalize_usage_deepseek
    if profile == ProviderProfile.OPENROUTER:
        return normalize_usage_openrouter
    return normalize_usage_default


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
    # Kimi (Moonshot) is a proprietary API: it speaks OpenAI-style
    # response_format (incl. json_schema) but not the vLLM guided_json field.
    return profile not in (ProviderProfile.DEEPSEEK, ProviderProfile.KIMI)


def default_chat_path(profile: ProviderProfile) -> str | None:
    """Return the provider's non-standard chat path, if any."""
    if profile == ProviderProfile.ZAI:
        return "/chat/completions"
    return None


def normalize_tools_for_profile(
    profile: ProviderProfile, tools: list[dict[str, Any]] | None
) -> list[dict[str, Any]] | None:
    """Return provider-specific normalized tool definitions."""
    if not tools:
        return tools
    if profile == ProviderProfile.KIMI:
        return [_sanitize_kimi_tool_schema(tool) for tool in tools]
    if profile == ProviderProfile.MINIMAX:
        return [_ensure_minimax_parameters(tool) for tool in tools]
    return tools


def _sanitize_kimi_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Drop a redundant parent "type" declared beside "anyOf" for Moonshot.

    Moonshot's JSON Schema validator rejects a schema node that declares both
    "type" and a sibling "anyOf" ("type should be defined in anyOf items
    instead of the parent schema"), even when every anyOf branch already
    declares its own "type" -- as discriminated-union tool schemas (e.g.
    Claude Code's own agent/task tools) commonly do.
    """
    function = tool.get("function")
    if not isinstance(function, dict) or "parameters" not in function:
        return tool
    return {
        **tool,
        "function": {
            **function,
            "parameters": _strip_type_beside_anyof(function["parameters"]),
        },
    }


def _strip_type_beside_anyof(schema: Any) -> Any:
    """Recursively remove a parent "type" that sits next to "anyOf".

    Rather than deleting the parent ``type`` outright (which would loosen the
    schema when a branch relies on the parent for its only type constraint --
    object-only keywords like ``required``/``properties`` don't reject
    non-objects), push it down into any ``anyOf`` branch that doesn't already
    declare its own ``type``, then drop it from the parent. This preserves the
    original validation semantics while satisfying Moonshot's rule that ``type``
    live in the ``anyOf`` items instead of beside them. The push-down runs
    before recursion so a branch that gains a ``type`` next to its own nested
    ``anyOf`` is normalized on the way down.
    """
    if isinstance(schema, dict):
        node = dict(schema)
        if "anyOf" in node and "type" in node and isinstance(node["anyOf"], list):
            parent_type = node["type"]
            node["anyOf"] = [
                {"type": parent_type, **branch}
                if isinstance(branch, dict) and "type" not in branch
                else branch
                for branch in node["anyOf"]
            ]
            del node["type"]
        return {key: _strip_type_beside_anyof(value) for key, value in node.items()}
    if isinstance(schema, list):
        return [_strip_type_beside_anyof(item) for item in schema]
    return schema


def _ensure_minimax_parameters(tool: dict[str, Any]) -> dict[str, Any]:
    """Default a missing/empty "parameters" schema for MiniMax.

    MiniMax rejects tool definitions with no parameters schema at all
    ("invalid params, function name or parameters is empty"), even though
    omitting "parameters" for a no-argument tool is valid per the OpenAI spec
    that other providers accept as-is.
    """
    function = tool.get("function")
    if not isinstance(function, dict) or function.get("parameters"):
        return tool
    return {
        **tool,
        "function": {**function, "parameters": {"type": "object", "properties": {}}},
    }


def normalize_messages_for_profile(
    profile: ProviderProfile, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return provider-specific normalized request messages.

    MiniMax strictly requires that every assistant message carrying
    ``tool_calls`` be immediately followed by ``tool`` messages answering all
    of its ids, and that every ``tool`` message answer the immediately
    preceding assistant's ``tool_calls``; anything else is rejected with
    "invalid params, tool call result does not follow tool call (2013)".
    Real agent-client histories routinely violate this (results delivered many
    turns later, or re-sent without their originating call) and every other
    provider accepts them, so for the MINIMAX profile the gateway sanitizes
    the history instead of letting the upstream 400. All other profiles pass
    through unchanged.
    """
    if profile != ProviderProfile.MINIMAX or not messages:
        return messages
    return _enforce_minimax_tool_adjacency(messages)


def _enforce_minimax_tool_adjacency(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop orphan tool_calls and downgrade orphan tool results for MiniMax.

    Copy-on-write: the input list and its message dicts are never mutated.

    - An assistant ``tool_calls`` entry survives only if a ``tool`` message in
      the contiguous run immediately following that assistant answers its id.
      If some entries are orphaned the list is filtered; if none survive the
      ``tool_calls`` key is removed, and an assistant message left without
      content is dropped entirely.
    - A ``tool`` message survives only if its governing assistant (the nearest
      preceding non-tool message) kept a matching tool_call id; otherwise it
      is downgraded to a plain user message carrying the same content.
    """
    # Pass 1: for each assistant tool_calls message, keep only the ids
    # answered by the contiguous run of tool messages right after it.
    kept_ids_by_index: dict[int, set[Any]] = {}
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        answered: set[Any] = set()
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            answered.add(messages[j].get("tool_call_id"))
            j += 1
        kept_ids_by_index[i] = {
            call.get("id")
            for call in tool_calls
            if isinstance(call, dict) and call.get("id") in answered
        }

    # Pass 2: rebuild the history with orphans filtered or downgraded.
    normalized: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if i in kept_ids_by_index:
            kept_ids = kept_ids_by_index[i]
            tool_calls = msg["tool_calls"]
            kept = [
                call for call in tool_calls if isinstance(call, dict) and call.get("id") in kept_ids
            ]
            if len(kept) == len(tool_calls):
                normalized.append(msg)
            elif kept:
                normalized.append({**msg, "tool_calls": kept})
            else:
                stripped = {k: v for k, v in msg.items() if k != "tool_calls"}
                content = stripped.get("content")
                if content is None or content == "":
                    continue  # tool_calls-only turn: nothing left worth sending
                normalized.append(stripped)
        elif msg.get("role") == "tool":
            governing = i - 1
            while governing >= 0 and messages[governing].get("role") == "tool":
                governing -= 1
            if governing >= 0 and msg.get("tool_call_id") in kept_ids_by_index.get(governing, ()):
                normalized.append(msg)
            else:
                content = msg.get("content")
                normalized.append({"role": "user", "content": "" if content is None else content})
        else:
            normalized.append(msg)
    return normalized


def resolve_tool_choice_for_profile(profile: ProviderProfile, tool_choice: Any) -> Any:
    """Resolve provider-specific tool choice defaults."""
    if tool_choice is not None:
        return tool_choice
    return None


def get_stream_idle_timeout_seconds(profile: ProviderProfile) -> float | None:
    """Return the stream socket-read idle timeout in seconds, if configured."""
    del profile
    raw = os.getenv("STREAM_IDLE_TIMEOUT_SECONDS")
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


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
    """Standard OpenAI-compatible usage extraction.

    Uses extract_reasoning_tokens and extract_cache_tokens to handle nested
    provider-specific formats such as:
    - completion_tokens_details.reasoning_tokens (ZAI, MiniMax, OpenAI o1)
    - prompt_tokens_details.cached_tokens (ZAI, MiniMax, Chutes, OpenAI)
    in addition to top-level fields.
    """
    from .base import UsageInfo

    cache_read, cache_write = extract_cache_tokens(usage_data)

    return UsageInfo(
        prompt_tokens=usage_data.get("prompt_tokens", 0),
        completion_tokens=usage_data.get("completion_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
        reasoning_tokens=extract_reasoning_tokens(usage_data) or 0,
        cache_read_tokens=cache_read or 0,
        cache_write_tokens=cache_write or 0,
        cache_read_reported=cache_read is not None,
    )


def normalize_usage_deepseek(usage_data: dict[str, Any]) -> UsageInfo:
    """DeepSeek-specific usage extraction.

    DeepSeek returns prompt_cache_hit_tokens (cache reads) and
    prompt_cache_miss_tokens (uncached) alongside prompt_tokens. Per their
    API, prompt_tokens already includes the cached portion (OpenAI semantic),
    so we keep it as-is and just surface cache_read_tokens for separate
    rate-application in calculate_cost.
    """
    from .base import UsageInfo

    cache_hit = usage_data.get("prompt_cache_hit_tokens", 0) or 0
    cache_miss = usage_data.get("prompt_cache_miss_tokens", 0) or 0
    prompt_tokens_raw = usage_data.get("prompt_tokens", 0) or 0

    # If upstream omits prompt_tokens but provides hit+miss, reconstruct.
    prompt_tokens = prompt_tokens_raw if prompt_tokens_raw else (cache_hit + cache_miss)

    return UsageInfo(
        prompt_tokens=prompt_tokens,
        completion_tokens=usage_data.get("completion_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
        reasoning_tokens=usage_data.get("reasoning_tokens", 0),
        cache_read_tokens=cache_hit,
        cache_write_tokens=usage_data.get("cache_creation_input_tokens", 0)
        or usage_data.get("cache_write_tokens", 0),
        cache_read_reported=(
            "prompt_cache_hit_tokens" in usage_data or "prompt_cache_miss_tokens" in usage_data
        ),
    )


def normalize_usage_openrouter(usage_data: dict[str, Any]) -> UsageInfo:
    """Extract usage info from an OpenRouter response (tokens + optional cost).

    OpenRouter reports `cost` (USD, per-request) when the request body sets
    `usage: {include: true}`. Cache and reasoning tokens (flat or nested under
    prompt_tokens_details / completion_tokens_details) are normalized by
    normalize_usage_default via the shared token_utils extractors.
    """
    base = normalize_usage_default(usage_data)

    cost = usage_data.get("cost")
    if cost is not None:
        try:
            parsed_cost = float(cost)
        except (TypeError, ValueError):
            logger.warning(
                "OpenRouter returned non-numeric cost %r (type=%s); upstream_cost_usd left null",
                cost,
                type(cost).__name__,
            )
        else:
            if not math.isfinite(parsed_cost):
                logger.warning("OpenRouter returned non-finite cost %r; ignoring", cost)
            elif parsed_cost < 0:
                logger.warning("OpenRouter returned negative cost %r; ignoring", cost)
            else:
                base.upstream_cost_usd = parsed_cost
    return base
