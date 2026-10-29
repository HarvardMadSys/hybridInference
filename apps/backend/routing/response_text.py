"""Shared extraction helpers for completion response text and usage."""

from __future__ import annotations

import json
from typing import Any


def response_text(response: Any) -> str | None:
    """Extract generated content from a non-streaming response body.

    A tool call is returned as a non-empty sentinel so the classifier treats it
    as real work even with no prose and no reported usage.
    """
    if not isinstance(response, dict):
        return None
    choices = response.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    content = (
        message.get("content")
        or message.get("reasoning_content")
        or message.get("reasoning")
        or message.get("thinking")
    )
    if isinstance(content, str) and content:
        return content
    if message.get("tool_calls") or message.get("function_call"):
        return "[tool_call]"
    return None


def response_usage(response: Any) -> dict[str, Any] | None:
    """Extract usage from a response body, or None when absent."""
    if not isinstance(response, dict):
        return None
    usage = response.get("usage")
    return usage if isinstance(usage, dict) else None


def chunk_text(chunk: Any) -> str | None:
    """Extract generated text from a stream chunk for outcome classification."""
    if not isinstance(chunk, str | bytes):
        return None
    text = chunk.decode("utf-8", errors="surrogatepass") if isinstance(chunk, bytes) else chunk
    prefix = "data: "
    if not text.startswith(prefix):
        return None
    try:
        obj = json.loads(text[len(prefix) :].strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    choices = obj.get("choices") or []
    delta = (choices[0] or {}).get("delta") or {} if choices else {}
    content = (
        delta.get("content")
        or delta.get("reasoning_content")
        or delta.get("reasoning")
        or delta.get("thinking")
    )
    if isinstance(content, str) and content:
        return content
    if delta.get("tool_calls") or delta.get("function_call"):
        return "[tool_call]"
    return None


def accumulate_stream_prefix(chunks: list[Any], max_chars: int = 512) -> str | None:
    """Accumulate generated text from the first content-bearing stream chunks.

    A warmup notice can be split across multiple SSE chunks. Classifying the
    first content-bearing chunk alone would read "The" as PROGRESS before the
    remaining text reveals the response is a warmup notice. This returns the
    concatenated text from the chunks seen so far, bounded to avoid unbounded
    accumulation, so the classifier can decide from the response prefix.
    """
    parts: list[str] = []
    total = 0
    for chunk in chunks:
        text = chunk_text(chunk)
        if text:
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
    if not parts:
        return None
    return "".join(parts)
