"""Token estimation utilities.

This module centralizes token counting logic so all adapters use the
same heuristics. It prefers tiktoken when available and falls back to
character-based estimation (4 chars ≈ 1 token). These functions are
pure and do not depend on adapter types to avoid circular imports.
"""

from __future__ import annotations


def _count_with_tiktoken(text: str) -> int:
    """Count tokens in a string using tiktoken if available.

    Falls back to character heuristic if tiktoken is not installed.
    """
    try:
        import tiktoken  # type: ignore

        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        # 4 characters ≈ 1 token (rough heuristic)
        return max(1, len(text) // 4)


def estimate_text_tokens(text: str) -> int:
    """Estimate token count for a plain text string.

    Uses tiktoken when available; otherwise a character-based heuristic.
    """
    if not text:
        return 0
    return _count_with_tiktoken(text)


def estimate_prompt_tokens(messages: list[dict[str, str]]) -> int:
    """Estimate token count for an OpenAI-style messages list.

    We approximate the per-message overhead and roles to stay consistent
    with common counting approaches.
    """
    if not messages:
        return 0

    # Base per-message overhead heuristic.
    overhead_per_message = 4
    overhead_end = 3

    total = 0
    for m in messages:
        role = str(m.get("role", ""))
        content = str(m.get("content", ""))
        total += estimate_text_tokens(role)
        total += estimate_text_tokens(content)
        total += overhead_per_message

    total += overhead_end
    return total


def estimate_total_tokens(messages: list[dict[str, str]], max_tokens: int | None = None) -> int:
    """Estimate total tokens for a request (prompt + completion budget)."""
    prompt_tokens = estimate_prompt_tokens(messages)
    completion_budget = max_tokens if max_tokens is not None else 500
    return prompt_tokens + int(completion_budget)
