"""SSE stream formatting helpers shared across adapters.

These helpers generate OpenAI-compatible SSE chunks for streaming
chat completions. Keeping this logic in one place ensures adapters
emit consistent formats and minimizes duplication.

:func:`pump_stream` and :class:`StreamQueue` are the reader a streaming
response consumes its upstream through, shared by the surfaces that stream.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)

#: How far a stream's reader may run ahead of the response it feeds, in chunks
#: and in bytes of them. A client that stops reading then stops the reads from
#: the upstream as well, through TCP, instead of making the gateway buffer the
#: rest of a generation that may run for an hour.
STREAM_QUEUE_MAX_ITEMS = 256
STREAM_QUEUE_MAX_BYTES = 1 << 20


class StreamQueue(asyncio.Queue):
    """A queue of stream chunks, full at so many chunks or so many bytes of them.

    A chunk's size is its length; anything else (an exception, an end marker)
    counts toward the item bound only. A chunk larger than the byte bound
    still goes in once the queue holds less than that, so a put never waits on
    a chunk that could not fit.
    """

    def __init__(
        self, *, max_items: int = STREAM_QUEUE_MAX_ITEMS, max_bytes: int = STREAM_QUEUE_MAX_BYTES
    ) -> None:
        """Bound the queue at *max_items* chunks or *max_bytes* bytes, whichever comes first."""
        super().__init__(maxsize=max_items)
        self._max_bytes = max_bytes
        self._bytes = 0

    def full(self) -> bool:
        """Whether a put has to wait for the consumer to take something."""
        return self._bytes >= self._max_bytes or super().full()

    def _put(self, item: Any) -> None:
        self._bytes += _chunk_size(item)
        super()._put(item)

    def _get(self) -> Any:
        item = super()._get()
        self._bytes -= _chunk_size(item)
        return item


def _chunk_size(item: object) -> int:
    return len(item) if isinstance(item, str | bytes | bytearray) else 0


async def pump_stream(source: AsyncIterator[Any], queue: asyncio.Queue, *, end: object) -> None:
    """Read *source* into *queue*: each chunk, an exception as an item, then *end*.

    Run as the task a streaming response reads its upstream in, so that a
    keepalive can go out while the upstream is quiet. With a bounded queue
    (:class:`StreamQueue`) the reads pause while the response takes no chunks.

    The response stops this task by cancelling it, once it is done or its
    client is gone. Nothing more is put then -- a put on a full queue nobody
    drains would never return -- and *source* is closed, which ends the
    upstream request and gives back what was held for it (its outbound slot, a
    lease) even when the task was waiting on the queue rather than in *source*.
    A cancellation *source* raises of its own accord, with nobody cancelling
    this task, ends the stream as its end does, as it always has.
    """
    try:
        try:
            async for item in source:
                await queue.put(item)
        except Exception as exc:
            await queue.put(exc)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is None or task.cancelling():
                raise
            await queue.put(end)
            raise
        await queue.put(end)
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                logger.warning("Closing a stream's upstream failed", exc_info=True)


def new_completion_id() -> str:
    """Return an id identifying one completion for its whole lifetime.

    OpenAI gives every chunk of a streamed completion the same ``id``, and
    clients group or trace frames by it. Minting one per chunk therefore breaks
    grouping just as surely as the millisecond timestamps this replaced broke
    it by colliding across concurrent streams: the id has to be unique between
    completions and constant within one.

    Callers own a completion's id and pass it to every builder below; the
    default exists only so a lone chunk built outside a stream is still valid.
    """
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def stamp_completion_id(chunk_json: dict[str, Any], completion_id: str) -> dict[str, Any]:
    """Set ``completion_id`` on a chunk an adapter minted with its own id.

    Adapters build frames without knowing which completion they belong to, so
    the router relabels them on the way out. Non-chunk payloads (errors, the
    synthetic routing frame) are left alone.
    """
    if chunk_json.get("object") == "chat.completion.chunk":
        chunk_json["id"] = completion_id
    return chunk_json


def make_stream_chunk(
    *,
    model: str,
    content: str = "",
    finish_reason: str | None = None,
    role: str | None = None,
    completion_id: str | None = None,
) -> str:
    """Create a single SSE data line for a chat.completion.chunk.

    Args:
        model: Model identifier to emit in the chunk.
        content: Delta content for this chunk; empty for terminal chunks.
        finish_reason: When provided, marks the final chunk finish reason.
        role: Role for the first chunk (e.g., "assistant"). OpenAI spec requires the first chunk to include role.
        completion_id: Id shared by every chunk of this completion. Defaults to
            a fresh one, which is correct only for a standalone chunk.

    Returns:
        A string representing one SSE line with a trailing blank line.
    """
    delta: dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content

    # If delta is empty and no finish_reason, at least include empty content
    if not delta and not finish_reason:
        delta = {"content": content}

    chunk: dict[str, Any] = {
        "id": completion_id or new_completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
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
    completion_tokens_override: int | None = None,
    finish_reason: str = "stop",
    provider: str | None = None,
    base_url: str | None = None,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_reported: bool = False,
    completion_id: str | None = None,
) -> str:
    """Create the final SSE chunk carrying usage metrics.

    Computes prompt/completion tokens if not provided by the caller and returns
    a properly formatted OpenAI-compatible SSE data line containing the usage
    object and a terminal choice with the finish_reason.

    Args:
        model: Model identifier to emit in the chunk.
        messages: Chat messages for token estimation.
        total_content: Generated content for token estimation.
        prompt_tokens_override: Override for prompt tokens (if available from provider).
        completion_tokens_override: Override for completion tokens (if available from provider).
        finish_reason: Finish reason for the completion.
        provider: Provider name for routing info (enables cost calculation).
        base_url: Provider base URL for routing info (enables cost calculation).
        reasoning_tokens: Reasoning tokens count (for models like GPT-5/o1/DeepSeek-R1).
        cache_read_tokens: Tokens read from cache (for cost calculation).
        cache_write_tokens: Tokens written to cache (for cost calculation).
        cache_read_reported: Whether the provider explicitly reported cache-read usage.
        completion_id: Id shared by every chunk of this completion. Defaults to
            a fresh one, which is correct only for a standalone chunk.
    """
    prompt_tokens = (
        int(prompt_tokens_override)
        if prompt_tokens_override is not None and prompt_tokens_override > 0
        else int(estimate_prompt_tokens(messages))
    )
    completion_tokens = (
        int(completion_tokens_override)
        if completion_tokens_override is not None and completion_tokens_override > 0
        else int(estimate_text_tokens(total_content))
    )

    usage: dict[str, int] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }

    # Add optional token fields if present
    if reasoning_tokens > 0:
        usage["reasoning_tokens"] = reasoning_tokens
    # Emit cache_read even at an explicitly-reported 0 so downstream can tell a
    # reported miss from "not reported" (see UsageInfo.to_dict).
    if cache_read_reported or cache_read_tokens > 0:
        usage["cache_read_tokens"] = cache_read_tokens
    if cache_write_tokens > 0:
        usage["cache_write_tokens"] = cache_write_tokens

    chunk: dict[str, Any] = {
        "id": completion_id or new_completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": usage,
    }

    # Include routing info for cost calculation if provider is specified
    if provider:
        chunk["_routing"] = {
            "provider": provider,
            "base_url": base_url,
        }

    return f"data: {json.dumps(chunk)}\n\n"


def make_role_chunk(*, model: str, completion_id: str | None = None) -> str:
    """Create the initial SSE chunk specifying assistant role.

    Many OpenAI-compatible clients expect the first streaming chunk to include
    a delta with "role": "assistant" to mark the beginning of the AI message.
    This helper emits that role-only chunk without content.

    ``completion_id`` is the id every later chunk of this completion repeats;
    it defaults to a fresh one only so a standalone chunk stays valid.
    """
    chunk: dict[str, Any] = {
        "id": completion_id or new_completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant"},
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(chunk)}\n\n"
