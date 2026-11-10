"""Shared response extraction for completion outcome classification."""

from __future__ import annotations

from typing import Any


def response_text(response: Any) -> str | None:
    """Extract generated content from a non-streaming response body.

    A tool call is represented by a non-empty sentinel so the classifier
    treats a structured call as real work even when it has no prose or usage.
    """
    if not isinstance(response, dict):
        return None
    choices = response.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content") or message.get("reasoning_content")
    if isinstance(content, str) and content:
        return content
    if message.get("tool_calls"):
        return "[tool_call]"
    return None
