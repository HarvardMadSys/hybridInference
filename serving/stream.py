"""SSE stream formatting helpers shared across adapters.

These helpers generate OpenAI-compatible SSE chunks for streaming
chat completions. Keeping this logic in one place ensures adapters
emit consistent formats and minimizes duplication.
"""

from __future__ import annotations

import json
import time
from typing import Any

from utils.tokens import estimate_prompt_tokens, estimate_text_tokens


def make_stream_chunk(
    *,
    model: str,
    content: str = "",
    finish_reason: str | None = None,
) -> str:
    """Create a single SSE data line for a chat.completion.chunk.

    Args:
        model: Model identifier to emit in the chunk.
        content: Delta content for this chunk; empty for terminal chunks.
        finish_reason: When provided, marks the final chunk finish reason.
    Returns:
        A string representing one SSE line with a trailing blank line.
    """
    chunk: dict[str, Any] = {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def done_sentinel() -> str:
    """Return the SSE stream termination sentinel line."""
    return "data: [DONE]\n\n"


def make_final_usage_chunk(
    *,
    model: str,
    messages: list[dict[str, Any]],
    total_content: str,
    prompt_tokens_override: int | None = None,
    finish_reason: str = "stop",
) -> str:
    """Create the final SSE chunk carrying usage metrics.

    Computes prompt/completion tokens if not provided by the caller and returns
    a properly formatted OpenAI-compatible SSE data line containing the usage
    object and a terminal choice with the finish_reason.
    """
    prompt_tokens = (
        int(prompt_tokens_override)
        if prompt_tokens_override is not None and prompt_tokens_override > 0
        else int(estimate_prompt_tokens(messages))
    )
    completion_tokens = int(estimate_text_tokens(total_content))
    chunk: dict[str, Any] = {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return f"data: {json.dumps(chunk)}\n\n"
