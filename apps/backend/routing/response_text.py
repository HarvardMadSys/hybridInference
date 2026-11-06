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


# Repeated scans of the same prefix (one per streamed chunk) re-parse the
# same SSE frames; cache the extraction so a long whitespace tail does not
# re-run json.loads O(n^2) times. Bounded so it cannot grow without limit.
_CHUNK_TEXT_CACHE: dict[Any, str | None] = {}
_CHUNK_TEXT_CACHE_MAX = 4096


def chunk_text(chunk: Any) -> str | None:
    """Extract generated text from a stream chunk for outcome classification."""
    if not isinstance(chunk, str | bytes):
        return None
    if chunk in _CHUNK_TEXT_CACHE:
        return _CHUNK_TEXT_CACHE[chunk]
    extracted = _extract_chunk_text(chunk)
    if len(_CHUNK_TEXT_CACHE) >= _CHUNK_TEXT_CACHE_MAX:
        _CHUNK_TEXT_CACHE.clear()
    _CHUNK_TEXT_CACHE[chunk] = extracted
    return extracted


def _extract_chunk_text(chunk: str | bytes) -> str | None:
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


class StreamPrefixAccumulator:
    """Bounded running prefix for incremental stream classification.

    ``observe`` appends one chunk's text and truncates at the cap; callers
    classify from :meth:`prefix` instead of re-scanning every chunk seen so
    far (which is O(n^2) JSON parsing over a long whitespace tail).
    """

    def __init__(self, max_chars: int = 512) -> None:
        self.max_chars = max_chars
        self._parts: list[str] = []
        self._total = 0

    @property
    def saturated(self) -> bool:
        return self._total >= self.max_chars

    def observe(self, chunk: Any) -> None:
        if self.saturated:
            return
        text = chunk_text(chunk)
        if not text:
            return
        room = self.max_chars - self._total
        self._parts.append(text[:room])
        self._total += min(len(text), room)

    def prefix(self) -> str | None:
        if not self._parts:
            return None
        return "".join(self._parts)


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
            room = max_chars - total
            parts.append(text[:room])
            total += min(len(text), room)
            if total >= max_chars:
                break
    if not parts:
        return None
    return "".join(parts)
