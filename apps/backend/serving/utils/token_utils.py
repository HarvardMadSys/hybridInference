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
    - usage["cached_tokens"] (SGLang, vLLM)
    - usage["cache_read_tokens"] (direct/normalized)
    - usage["prompt_cache_hit_tokens"] (DeepSeek)
    - usage["prompt_tokens_details"]["cached_tokens"] (OpenAI / Azure)
    - usage["input_tokens_details"]["cached_tokens"] (MiniMax)
    - usage["input_token_details"]["cached_tokens"] (MiniMax)
    - usage["*_tokens_details"]["cache_read_tokens"] (MiniMax)
    - usage["*_tokens_details"]["cache_hit_tokens"] (MiniMax)
    - usage["cache_creation_input_tokens"] (Anthropic Claude write)
    - usage["cache_write_tokens"] (direct/normalized)

    A ``*_tokens_details`` key present but null counts as a reported 0 (sglang
    and vLLM answer a prefix-cache miss that way).

    Args:
        usage: Usage dictionary from model response

    Returns:
        Tuple of (cache_read_tokens, cache_write_tokens), each int or None.
        ``None`` means the provider reported nothing about caching; ``0`` means
        it reported a miss. Callers distinguish the two -- ``UsageInfo`` drops
        the field for ``None`` and ``api_logs.cache_read_tokens`` stores NULL --
        so a provider that cannot report cache usage is not scored as a miss.
    """
    if not usage or not isinstance(usage, dict):
        return None, None

    # --- cache read tokens ---
    cache_read: int | None = None

    # Direct fields (Anthropic style, then SGLang/vLLM, then generic, then DeepSeek).
    # val >= 0 so that an explicit 0 ("cache supported but no hit") is recorded
    # rather than falling through to the next field or returning None.
    for field in (
        "cache_read_input_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "prompt_cache_hit_tokens",
    ):
        val = usage.get(field)
        if val is not None:
            try:
                val = int(val)
                if val >= 0:
                    cache_read = val
                    break
            except (TypeError, ValueError):
                pass

    # Nested: OpenAI/Azure use prompt_tokens_details; MiniMax may use input token details.
    # A details key carried as null is the provider *reporting* that nothing was
    # cached: sglang (with --enable-cache-report) and vLLM answer a prefix-cache
    # miss with `"prompt_tokens_details": null` rather than `{"cached_tokens": 0}`.
    # That is the same "supported but no hit" the direct-field branch above already
    # records as 0, so record it as 0 here too -- otherwise a reported miss is
    # filed as an unmeasured request and `None` stops meaning the one thing the
    # rest of the system reads it as (`cache_read_reported=False`, the field
    # dropped from `UsageInfo.to_dict()`, NULL in `api_logs.cache_read_tokens`),
    # which is a provider that says nothing about caching at all. Applied only
    # after every field has had its turn, so a null `prompt_tokens_details` cannot
    # mask a populated `input_tokens_details` on a provider that sends both.
    reported_no_cache = False
    for details_field in (
        "prompt_tokens_details",
        "input_tokens_details",
        "input_token_details",
    ):
        if cache_read is not None or details_field not in usage:
            continue
        details = usage[details_field]
        if details is None:
            reported_no_cache = True
            continue
        if isinstance(details, dict):
            for nested_field in ("cached_tokens", "cache_read_tokens", "cache_hit_tokens"):
                val = details.get(nested_field)
                if val is None:
                    continue
                try:
                    val = int(val)
                    if val >= 0:
                        cache_read = val
                        break
                except (TypeError, ValueError):
                    pass
            if cache_read is not None:
                break

    if cache_read is None and reported_no_cache:
        cache_read = 0

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
