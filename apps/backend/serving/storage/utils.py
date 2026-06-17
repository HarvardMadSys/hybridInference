"""Pure utility functions for the storage layer.

These functions have no database dependencies and can be imported safely
without constructing storage clients.
"""

from __future__ import annotations

import json
import math
from typing import Any


def json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats with ``None`` for JSON storage."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    return value


def conversation_shape(
    prompt: list[dict[str, Any]] | str | None,
) -> tuple[int | None, int | None, int | None]:
    """Derive ``(num_turns, num_user_turns, num_tool_calls)`` from a prompt.

    Computed once at log time so the admin list query can read three cheap
    integer columns instead of de-TOASTing the full request payload per row.
    ``num_turns`` counts all messages, ``num_user_turns`` counts user-role
    messages, and ``num_tool_calls`` sums ``tool_calls`` across messages.

    Returns ``(None, None, None)`` when ``prompt`` is not a chat-style messages
    list (e.g. a raw completion string or an embedding input).
    """
    if not isinstance(prompt, list):
        return None, None, None
    num_turns = 0
    num_user_turns = 0
    num_tool_calls = 0
    for message in prompt:
        if not isinstance(message, dict):
            continue
        num_turns += 1
        if message.get("role") == "user":
            num_user_turns += 1
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            num_tool_calls += len(tool_calls)
    return num_turns, num_user_turns, num_tool_calls


def strip_null_bytes(value: Any) -> Any:
    """Recursively remove PostgreSQL-incompatible null bytes from strings."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {
            strip_null_bytes(k) if isinstance(k, str) else k: strip_null_bytes(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [strip_null_bytes(v) for v in value]
    if isinstance(value, tuple):
        return [strip_null_bytes(v) for v in value]
    return value


def coerce_json_object(value: Any) -> dict[str, Any] | None:
    """Return a JSON object from decoded JSON/JSONB values, or None for non-objects."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def calculate_cost(
    usage: dict[str, Any] | None,
    pricing: dict[str, str] | None,
) -> float | None:
    """Compute request cost in USD based on usage and pricing tables.

    Uses OpenAI semantics: ``prompt_tokens`` is the *total* input including
    any cached portion. The cached subset is reported separately in
    ``cache_read_tokens`` / ``cache_write_tokens`` and billed at its own
    rate, so we subtract it from ``prompt_tokens`` before applying
    ``prompt_price`` to avoid double-charging. Cached tokens are only
    subtracted when a specific cache price is configured (>0); otherwise
    they fall back to being billed at the regular prompt rate so models
    that report cache tokens but lack cache-specific pricing aren't
    silently under-billed.
    """
    if not usage or not pricing:
        return None

    try:
        prompt_tokens = float(usage.get("prompt_tokens", 0))
        completion_tokens = float(usage.get("completion_tokens", 0))
        reasoning_tokens = float(usage.get("reasoning_tokens", 0))
        cache_read_tokens = float(usage.get("cache_read_tokens", 0))
        cache_write_tokens = float(usage.get("cache_write_tokens", 0))

        prompt_price = float(pricing.get("prompt", "0"))
        completion_price = float(pricing.get("completion", "0"))
        cache_read_price = float(pricing.get("input_cache_reads", "0"))
        cache_write_price = float(pricing.get("input_cache_writes", "0"))

        billable_prompt_tokens = prompt_tokens
        if cache_read_price > 0:
            billable_prompt_tokens -= cache_read_tokens
        if cache_write_price > 0:
            billable_prompt_tokens -= cache_write_tokens
        billable_prompt_tokens = max(billable_prompt_tokens, 0.0)

        return (
            (billable_prompt_tokens * prompt_price / 1_000_000)
            + (completion_tokens * completion_price / 1_000_000)
            + (reasoning_tokens * completion_price / 1_000_000)
            + (cache_read_tokens * cache_read_price / 1_000_000)
            + (cache_write_tokens * cache_write_price / 1_000_000)
        )
    except (ValueError, TypeError):
        return None
