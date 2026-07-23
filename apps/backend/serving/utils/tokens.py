"""Token estimation utilities used across serving components.

This module centralizes token counting logic so that adapters, streaming
formatters, and rate limiters share the same estimation behavior.
"""

from __future__ import annotations

from typing import Any

import tiktoken


def _get_tiktoken_encoding():
    """Get tiktoken encoding, with fallback handling.

    Returns:
        tiktoken.Encoding object or None if tiktoken is not available.
    """
    return tiktoken.get_encoding("cl100k_base")


def _count_with_tiktoken(text: str) -> int:
    """Count tokens for a string using tiktoken when available.

    Falls back to a simple character-based heuristic when tiktoken is not
    installed. The heuristic assumes approximately 4 characters per token.

    Args:
      text: Input string to count.

    Returns:
      Estimated token count (>= 1 for non-empty strings).
    """
    encoding = _get_tiktoken_encoding()
    if encoding:
        return len(encoding.encode(text))
    else:
        # 4 characters ≈ 1 token (rough heuristic)
        return max(1, len(text) // 4)


def tokenize_text(text: str) -> list[int]:
    """Tokenize text into token IDs using tiktoken.

    Falls back to character-based chunking if tiktoken is not available.

    Args:
        text: Input string to tokenize.

    Returns:
        List of token IDs. If tiktoken is unavailable, returns character codes
        grouped by 4 (simulating tokens).
    """
    if not text:
        return []

    encoding = _get_tiktoken_encoding()
    if encoding:
        return encoding.encode(text)
    else:
        # Fallback: use character codes grouped by 4 as pseudo-tokens
        # This ensures consistent behavior even without tiktoken
        chars = text.encode("utf-8")
        tokens = []
        for i in range(0, len(chars), 4):
            # Combine 4 bytes into a single "token" ID
            chunk = chars[i : i + 4]
            token_id = int.from_bytes(chunk, byteorder="big", signed=False)
            tokens.append(token_id)
        return tokens


def estimate_text_tokens(text: str) -> int:
    """Estimate token count for a plain text string.

    Uses tiktoken when available; otherwise a character-based heuristic.

    Args:
      text: Input string to estimate.

    Returns:
      Estimated token count.
    """
    if not text:
        return 0
    return _count_with_tiktoken(text)


# Coarse per-modality fallback estimates for multimodal content blocks.
#
# Real image/audio token costs are provider-specific and arrive in the upstream
# ``usage`` payload; these constants only matter when we have to estimate
# locally (rate-limit pre-checks, RouteWise size hints, and usage fallbacks
# when a provider omits ``usage``). The critical point is to NEVER feed a
# base64 data URL / audio blob through the text tokenizer: a single inline
# image can be hundreds of kilobytes, which would otherwise be counted as tens
# of thousands of phantom text tokens and corrupt quota/cost accounting.
IMAGE_TOKEN_ESTIMATE = 85  # matches OpenAI's low-detail image base cost
AUDIO_TOKEN_ESTIMATE = 200  # coarse placeholder; precise counts come from upstream
VIDEO_TOKEN_ESTIMATE = 1000  # coarse placeholder; real cost scales with frames/fps upstream


def _estimate_block_tokens(block: Any) -> int:
    """Estimate tokens for a single content block within a message.

    Handles OpenAI-style structured blocks (``{"type": "text"|"image_url"|
    "input_audio", ...}``) as well as bare strings. Non-text blocks contribute
    a flat per-modality estimate rather than the size of their (often base64)
    payload.
    """
    if isinstance(block, str):
        return estimate_text_tokens(block)
    if not isinstance(block, dict):
        return 0

    block_type = block.get("type")
    # Text blocks: {"type": "text", "text": ...}; some clients omit the type.
    if block_type == "text" or (block_type is None and "text" in block):
        text = block.get("text")
        return estimate_text_tokens(text) if isinstance(text, str) else 0
    if block_type in ("image_url", "image", "input_image"):
        return IMAGE_TOKEN_ESTIMATE
    if block_type in ("input_audio", "audio"):
        return AUDIO_TOKEN_ESTIMATE
    if block_type in ("video_url", "video", "input_video"):
        return VIDEO_TOKEN_ESTIMATE

    # Unknown block: count any embedded text, but never the raw payload/blob.
    text = block.get("text")
    return estimate_text_tokens(text) if isinstance(text, str) else 0


def _estimate_content_tokens(content: Any) -> int:
    """Estimate tokens for a message ``content`` field of any shape.

    ``content`` may be a plain string (OpenAI text style), a list of structured
    blocks (multimodal style), a single block mapping, or ``None``.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, list):
        return sum(_estimate_block_tokens(block) for block in content)
    if isinstance(content, dict):
        return _estimate_block_tokens(content)
    return estimate_text_tokens(str(content))


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate token count for an OpenAI-style messages list.

    Each message contributes role + content tokens plus a small overhead to
    approximate protocol framing costs. This mirrors common counting approaches.

    Multimodal content (a list of text/image/audio blocks) is handled
    structurally: text blocks are tokenized while image/audio blocks add a flat
    per-modality estimate, so inline base64 payloads never inflate the count.

    Args:
      messages: Chat messages with ``role`` and ``content`` fields.

    Returns:
      Estimated prompt token count.
    """
    if not messages:
        return 0

    overhead_per_message = 4
    overhead_end = 3

    total = 0
    for m in messages:
        role = str(m.get("role", ""))
        total += estimate_text_tokens(role)
        total += _estimate_content_tokens(m.get("content"))
        total += overhead_per_message

    total += overhead_end
    return total


def estimate_total_tokens(messages: list[dict[str, Any]], max_tokens: int | None = None) -> int:
    """Estimate total request tokens (prompt + completion budget).

    Args:
      messages: Chat messages in OpenAI format.
      max_tokens: Optional completion budget; defaults to 500 if None.

    Returns:
      Estimated total tokens for the request.
    """
    prompt_tokens = estimate_prompt_tokens(messages)
    completion_budget = max_tokens if max_tokens is not None else 500
    return prompt_tokens + int(completion_budget)


__all__ = [
    "estimate_prompt_tokens",
    "estimate_text_tokens",
    "estimate_total_tokens",
    "tokenize_text",
]
