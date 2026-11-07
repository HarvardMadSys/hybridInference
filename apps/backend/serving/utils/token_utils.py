"""Utilities for extracting token usage from various response formats.

Different models handle reasoning/thinking tokens differently:
- OpenAI o1: reasoning_tokens in completion_tokens_details
- Gemini 2.5: thoughts_token_count in usage_metadata
- Qwen3 Coder: non-thinking mode (no explicit reasoning tokens)
"""

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

#: Token counts a usage block may carry: OpenAI, Anthropic and normalized names.
USAGE_COUNT_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _read_count(value: Any) -> int | None:
    """``value`` as a non-negative int, or None when it is not a readable count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number: float = value
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def usage_count(value: Any, *, field: str = "token count") -> int:
    """Read one upstream token count as a non-negative int; anything else is 0.

    An upstream that answered has been paid for, so a count we cannot read must
    not turn that answer into an error. The decided policy is to bill such a
    count as zero and log it, so the provider sending it can be found. Numbers
    and numeric strings ("12", "12.0") are read; text, booleans, NaN, infinity
    and negative values are not. ``None`` (not reported) is 0 without a warning.
    """
    if value is None:
        return 0
    count = _read_count(value)
    if count is None:
        logger.warning("Unreadable upstream %s %r; billed as 0", field, value)
        return 0
    return count


def sanitize_usage(usage: dict[str, Any], *, complete: bool = False) -> dict[str, Any]:
    """Return a copy of *usage* whose counts are all readable (see :func:`usage_count`).

    An unreadable ``total_tokens`` becomes None ("not reported"), never a
    number derived here: billing reads a reported total as evidence of whether
    completion_tokens already includes reasoning
    (``serving.storage.utils.billable_output_tokens``), so a made-up total would
    change what is charged. A ``*_details`` field that is not an object is
    dropped.

    ``complete=True`` is only for the copy of a usage block returned to a
    client, whose model requires all three totals: a missing prompt or
    completion count becomes 0 and a missing total their sum. Never bill from it.
    """
    clean = dict(usage)
    if complete:
        for field in ("prompt_tokens", "completion_tokens"):
            if clean.get(field) is None:
                clean[field] = 0
    for field in USAGE_COUNT_FIELDS:
        if clean.get(field) is not None:
            clean[field] = usage_count(clean[field], field=field)
    total = clean.get("total_tokens")
    if total is not None:
        count = _read_count(total)
        if count is None:
            logger.warning("Unreadable upstream total_tokens %r; treated as not reported", total)
        clean["total_tokens"] = count
    if complete and clean.get("total_tokens") is None:
        clean["total_tokens"] = clean["prompt_tokens"] + clean["completion_tokens"]
    for field in [name for name in clean if name.endswith("_details")]:
        if clean[field] is not None and not isinstance(clean[field], dict):
            logger.warning("Unreadable upstream usage %s %r; dropped", field, clean[field])
            del clean[field]
    return clean


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


def optional_token_count(value: Any) -> int | None:
    """Coerce a reported token count to ``int``, preserving "not reported".

    A provider that omits a cache-token field is *not* reporting a measured
    zero, and the two must not be conflated: ``None`` flows through to a NULL
    ``api_logs`` column and an "unknown" cache state, whereas ``0`` is a
    measured miss that a dashboard renders as 0% and a cost model reads as
    "no discount earned". Collapsing ``None`` to ``0`` at a call site is how a
    silent provider comes to look like a cache miss.

    Returns ``None`` for a missing key, ``None``, a non-numeric value, a
    negative count, a fractional value, or a non-finite one (``inf``/``nan``,
    which ``int()`` would otherwise raise on or silently truncate); otherwise
    the value as ``int``. A reported ``0`` stays ``0`` -- it is a real
    measurement. Accepting a bare ``int`` fast-path keeps the common case off
    the float path.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass; a boolean is never a token count.
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if number != int(number):
        # A fractional token count is not a real measurement; treat it as
        # unreported rather than silently truncating.
        return None
    return int(number)


#: Direct cache-count fields a usage block may carry, read by
#: :func:`extract_cache_tokens`: Anthropic, SGLang/vLLM, normalized, DeepSeek.
_CACHE_READ_FIELDS = (
    "cache_read_input_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "prompt_cache_hit_tokens",
)
_CACHE_WRITE_FIELDS = ("cache_creation_input_tokens", "cache_write_tokens")


def extract_cache_tokens(
    usage: dict[str, Any] | None,
    *,
    null_details_means_miss: bool = False,
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

    Args:
        usage: Usage dictionary from model response
        null_details_means_miss: Whether a ``*_tokens_details`` key that is
            present but null may be read as a reported 0. Off by default,
            because that null is ambiguous on the wire (see the note above the
            nested-details loop): only a caller holding route configuration --
            ``ModelConfig.null_cache_details_means_miss`` -- knows whether the
            endpoint reports cache usage at all, so only it can license the 0.

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
    for field in _CACHE_READ_FIELDS:
        count = optional_token_count(usage.get(field))
        if count is not None:
            cache_read = count
            break

    # Nested: OpenAI/Azure use prompt_tokens_details; MiniMax may use input token details.
    #
    # A details key carried as null is ambiguous, and nothing in the response
    # resolves it. sglang started with --enable-cache-report answers a
    # prefix-cache *miss* with `"prompt_tokens_details": null` rather than
    # `{"cached_tokens": 0}` -- a reported 0, which the direct-field branch
    # above would have recorded as 0. But sglang started *without* that flag,
    # and vLLM without --enable-prompt-tokens-details, send the identical null
    # on every request, hit or miss (vllm-project/vllm#44377); there the null
    # means "this server does not report cache usage".
    #
    # Reading it as 0 unconditionally would turn the second case into a
    # fabricated measurement -- worse than the missing value it replaces,
    # because `None` is the one thing the rest of the system reads as "nothing
    # was reported" (`cache_read_reported=False`, the field dropped from
    # `UsageInfo.to_dict()`, NULL in `api_logs.cache_read_tokens`). So the 0 is
    # only taken when the caller passes ``null_details_means_miss``, i.e. when
    # route configuration says this endpoint reports.
    saw_null_details = False
    for details_field in (
        "prompt_tokens_details",
        "input_tokens_details",
        "input_token_details",
    ):
        if cache_read is not None or details_field not in usage:
            continue
        details = usage[details_field]
        if details is None:
            saw_null_details = True
            continue
        if isinstance(details, dict):
            for nested_field in ("cached_tokens", "cache_read_tokens", "cache_hit_tokens"):
                count = optional_token_count(details.get(nested_field))
                if count is not None:
                    cache_read = count
                    break
            if cache_read is not None:
                break

    # Applied only after every details field has had its turn, so a null
    # `prompt_tokens_details` cannot mask a populated `input_tokens_details` on
    # a provider that sends both.
    if cache_read is None and saw_null_details and null_details_means_miss:
        cache_read = 0

    # --- cache write tokens ---
    cache_write: int | None = None

    for field in _CACHE_WRITE_FIELDS:
        count = optional_token_count(usage.get(field))
        if count is not None:
            cache_write = count
            break

    return cache_read, cache_write


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize usage dict by extracting and flattening token fields.

    Extracts reasoning and cache tokens from provider formats, flattens cache
    fields to the top level, and removes invalid direct cache counts before
    cost and persistence consumers read them.

    Args:
        usage: Raw usage dictionary from model response

    Returns:
        Normalized usage dict with reasoning/cache tokens at top level, or None
    """
    if not usage:
        return None
    if not isinstance(usage, dict):
        return None
    # An invalid direct cache count is *unreported*, not zero: drop it before
    # sanitize_usage coerces a negative, fractional or non-numeric count into
    # a readable -- and misleadingly measured -- 0 (or truncated int). The
    # nested ``*_details`` values keep their raw form here;
    # :func:`extract_cache_tokens` reads them through
    # :func:`optional_token_count` below.
    readable = {
        key: value
        for key, value in usage.items()
        if not (
            value is not None
            and (key in _CACHE_READ_FIELDS or key in _CACHE_WRITE_FIELDS)
            and optional_token_count(value) is None
        )
    }
    usage = sanitize_usage(readable)
    normalized = dict(usage)

    # Extract reasoning tokens from nested locations
    reasoning_tokens = extract_reasoning_tokens(usage)

    # Extract cache tokens from nested locations
    cache_read, cache_write = extract_cache_tokens(normalized)

    # Add reasoning_tokens at top level if found
    if reasoning_tokens is not None:
        normalized["reasoning_tokens"] = reasoning_tokens

    # Add cache tokens at top level if found
    if cache_read is not None:
        normalized["cache_read_tokens"] = cache_read
    if cache_write is not None:
        normalized["cache_write_tokens"] = cache_write

    return normalized
