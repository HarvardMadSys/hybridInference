"""Pure utility functions for the storage layer.

These functions have no database dependencies and can be imported safely
without constructing storage clients.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_prompt_hash(prompt: list[dict[str, Any]] | str) -> str:
    """Compute SHA256 hash of prompt for deduplication and caching.

    Args:
        prompt: Prompt messages (list of dicts) or string.

    Returns:
        Hex-encoded SHA256 hash (64 characters).
    """
    if isinstance(prompt, list | dict):
        prompt_str = json.dumps(prompt, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    else:
        prompt_str = str(prompt)

    return hashlib.sha256(prompt_str.encode("utf-8")).hexdigest()


def compute_prompt_hash_chunked(
    prompt: list[dict[str, Any]] | str,
    chunk_size: int = 4,
) -> str:
    """Compute hash of prompt using 4-token chunks for privacy protection.

    This function tokenizes the prompt and computes a hash for every N tokens
    (default 4), then combines all chunk hashes into a final hash.

    Args:
        prompt: Prompt messages (list of dicts) or string.
        chunk_size: Number of tokens per chunk (default: 4).

    Returns:
        Hex-encoded SHA256 hash of all chunk hashes combined.

    Raises:
        ValueError: If chunk_size is <= 0.
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")

    from serving.utils.tokens import tokenize_text

    if isinstance(prompt, list | dict):
        prompt_str = json.dumps(prompt, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    else:
        prompt_str = str(prompt)

    tokens = tokenize_text(prompt_str)

    if not tokens:
        return hashlib.sha256(b"").hexdigest()

    final_hasher = hashlib.sha256()
    for i in range(0, len(tokens), chunk_size):
        chunk = tokens[i : i + chunk_size]
        chunk_bytes = b"".join(
            token_id.to_bytes(4, byteorder="big", signed=False) for token_id in chunk
        )
        chunk_digest = hashlib.sha256(chunk_bytes).digest()
        final_hasher.update(chunk_digest)

    return final_hasher.hexdigest()


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
