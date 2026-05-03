"""Utilities for extracting token usage from various response formats.

Different models handle reasoning/thinking tokens differently:
- OpenAI o1: reasoning_tokens in completion_tokens_details
- Gemini 2.5: thoughts_token_count in usage_metadata
- Qwen3 Coder: non-thinking mode (no explicit reasoning tokens)
"""

from typing import Any


def extract_reasoning_tokens(usage: dict[str, Any] | None) -> int | None:
    """Extract reasoning tokens from various possible locations in usage dict.

    Different providers/models may put reasoning_tokens in different places:
    - usage["reasoning_tokens"] (direct field)
    - usage["completion_tokens_details"]["reasoning_tokens"] (OpenAI o1)
    - usage["thoughts_token_count"] (Gemini 2.5 thinking mode)
    - usage["thinking_tokens"] (alternative naming)
    - usage["prompt_tokens_details"]["reasoning_tokens"] (potential future format)

    Args:
        usage: Usage dictionary from model response

    Returns:
        Reasoning tokens count or None if not found
    """
    if not usage or not isinstance(usage, dict):
        return None

    # Direct field variations
    for field in ["reasoning_tokens", "thoughts_token_count", "thinking_tokens"]:
        if field in usage:
            try:
                return int(usage[field])
            except (TypeError, ValueError):
                pass

    # In completion_tokens_details (OpenAI o1 style)
    if "completion_tokens_details" in usage:
        details = usage["completion_tokens_details"]
        if isinstance(details, dict):
            for field in ["reasoning_tokens", "thinking_tokens"]:
                if field in details:
                    try:
                        return int(details[field])
                    except (TypeError, ValueError):
                        pass

    # In usage_metadata (Gemini style)
    if "usage_metadata" in usage:
        metadata = usage["usage_metadata"]
        if isinstance(metadata, dict):
            for field in ["thoughts_token_count", "thinking_tokens", "reasoning_tokens"]:
                if field in metadata:
                    try:
                        return int(metadata[field])
                    except (TypeError, ValueError):
                        pass

    # In prompt_tokens_details (potential future format)
    if "prompt_tokens_details" in usage:
        details = usage["prompt_tokens_details"]
        if isinstance(details, dict) and "reasoning_tokens" in details:
            try:
                return int(details["reasoning_tokens"])
            except (TypeError, ValueError):
                pass

    return None


def extract_cache_tokens(
    usage: dict[str, Any] | None,
) -> tuple[int | None, int | None]:
    """Extract cache read and write tokens from various provider formats.

    Different providers return cache token info in different locations:
    - usage["cache_read_input_tokens"] (Anthropic Claude)
    - usage["cache_read_tokens"] (direct/normalized)
    - usage["prompt_tokens_details"]["cached_tokens"] (OpenAI / Azure)
    - usage["prompt_cache_hit_tokens"] (DeepSeek)
    - usage["cache_creation_input_tokens"] (Anthropic Claude write)
    - usage["cache_write_tokens"] (direct/normalized)

    Args:
        usage: Usage dictionary from model response

    Returns:
        Tuple of (cache_read_tokens, cache_write_tokens), each int or None.
    """
    if not usage or not isinstance(usage, dict):
        return None, None

    # --- cache read tokens ---
    cache_read: int | None = None

    # Direct fields (Anthropic style, then generic).
    # val >= 0 so that an explicit 0 ("cache supported but no hit") is recorded
    # rather than falling through to the next field or returning None.
    for field in ("cache_read_input_tokens", "cache_read_tokens", "prompt_cache_hit_tokens"):
        val = usage.get(field)
        if val is not None:
            try:
                val = int(val)
                if val >= 0:
                    cache_read = val
                    break
            except (TypeError, ValueError):
                pass

    # Nested: prompt_tokens_details.cached_tokens (OpenAI / Azure style)
    if cache_read is None and "prompt_tokens_details" in usage:
        details = usage["prompt_tokens_details"]
        if isinstance(details, dict):
            val = details.get("cached_tokens")
            if val is not None:
                try:
                    val = int(val)
                    if val >= 0:
                        cache_read = val
                except (TypeError, ValueError):
                    pass

    # --- cache write tokens ---
    cache_write: int | None = None

    for field in ("cache_creation_input_tokens", "cache_write_tokens"):
        val = usage.get(field)
        if val is not None:
            try:
                val = int(val)
                if val >= 0:
                    cache_write = val
                    break
            except (TypeError, ValueError):
                pass

    return cache_read, cache_write


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize usage dict by extracting and flattening token fields.

    Extracts reasoning_tokens and cache tokens from various nested provider
    formats and places them at the top level for consistent downstream access.

    Args:
        usage: Raw usage dictionary from model response

    Returns:
        Normalized usage dict with reasoning/cache tokens at top level, or None
    """
    if not usage:
        return None

    # Extract reasoning tokens from nested locations
    reasoning_tokens = extract_reasoning_tokens(usage)

    # Extract cache tokens from nested locations
    cache_read, cache_write = extract_cache_tokens(usage)

    # Create normalized usage dict
    normalized = dict(usage)  # Copy to avoid modifying original

    # Add reasoning_tokens at top level if found
    if reasoning_tokens is not None:
        normalized["reasoning_tokens"] = reasoning_tokens

    # Add cache tokens at top level if found
    if cache_read is not None:
        normalized["cache_read_tokens"] = cache_read
    if cache_write is not None:
        normalized["cache_write_tokens"] = cache_write

    return normalized
