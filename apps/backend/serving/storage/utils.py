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
