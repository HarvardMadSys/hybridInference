"""Streaming helpers shared by routing implementations."""

from __future__ import annotations

from typing import Any


def has_non_empty_content(chunk: Any) -> bool:
    r"""Return True if the SSE ``chunk`` carries a non-empty delta.

    The streaming protocol emits lines like ``"data: {json}\n\n"`` and a
    terminal ``"data: [DONE]\n\n"``. We consider a chunk as having started
    output when delta.content, reasoning/thinking text, or a structured
    ``tool_calls``/legacy ``function_call`` delta is non-empty.
    """
    try:
        if not isinstance(chunk, str | bytes):
            return True  # Unknown type; assume it carries content
        s = chunk.decode() if isinstance(chunk, bytes) else chunk
        if "[DONE]" in s:
            return False
        prefix = "data: "
        if not s.startswith(prefix):
            return True  # Non-standard; assume content
        import json as _json

        payload = s[len(prefix) :].strip()
        obj = _json.loads(payload)
        choices = obj.get("choices") or []
        if not choices:
            return False
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and len(content) > 0:
            return True
        reasoning = (
            delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
        )
        if isinstance(reasoning, str) and len(reasoning) > 0:
            return True
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list) and len(tool_calls) > 0:
            return True
        return bool(delta.get("function_call"))
    except Exception:
        # Be conservative and treat as content to avoid missing TTFT altogether
        return True
