"""How many elements an inference request body may hold, checked before it is validated.

The body is bounded in bytes (``REQUEST_MAX_BODY_MB``, 32 MiB), but the work
the gateway does on it is per element, and 32 MiB holds about two million
``{"role":"user"}`` messages. Each became a Pydantic model, was dumped back to a
dict and was walked again by every later step, so one such request to an
unknown model held the event loop for 7-16 s before its 404. These limits are
checked on the decoded JSON, before Pydantic sees it; every list's length is
compared before the list is walked, so a check costs no more than the limits
allow, whatever was sent.

The limits are generous, far above what a real client sends, and a request at
every limit is served in well under a second of loop time (measured on the
chat route: 0.3 s for 50,000 messages, 0.7 s for 50,000 with two parts each).

* :data:`MAX_MESSAGES`: the largest context window served is about a million
  tokens. A coding agent's message averages hundreds of tokens (a tool result,
  a file), so a whole million-token session is a few thousand messages, and
  even a minimal tool round trip (a call and an "ok") takes some 40 tokens:
  50,000 messages do not fit in any context window served.
* :data:`MAX_PARTS_PER_MESSAGE` and :data:`MAX_PARTS_PER_REQUEST`: content
  parts (text, images, tool results) and tool calls. A message holds a few, a
  turn after many parallel tool calls a few dozen: 10,000 in one message, and
  100,000 in a request, two per message at the message limit.
* :data:`MAX_TOOLS`: agents send a few dozen tools, MCP-heavy setups a few
  hundred; OpenAI's Chat Completions accepts at most 128.
* :data:`MAX_STOP_SEQUENCES`: OpenAI accepts at most 4.

A request over a limit is answered 400, naming the limit, in the surface's own
error shape.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MAX_MESSAGES",
    "MAX_PARTS_PER_MESSAGE",
    "MAX_PARTS_PER_REQUEST",
    "MAX_STOP_SEQUENCES",
    "MAX_TOOLS",
    "chat_limit_problem",
    "messages_limit_problem",
    "responses_limit_problem",
]

MAX_MESSAGES = 50_000
MAX_PARTS_PER_MESSAGE = 10_000
MAX_PARTS_PER_REQUEST = 100_000
MAX_TOOLS = 2_000
MAX_STOP_SEQUENCES = 64


def chat_limit_problem(body: Any) -> str | None:
    """Describe the first count in a Chat Completions body over its limit, or None.

    Counts ``messages``, each message's ``content`` parts and ``tool_calls``,
    ``tools`` and ``stop``. A field of another type is left to validation.
    """
    if not isinstance(body, dict):
        return None
    return (
        _length_problem(body.get("messages"), MAX_MESSAGES, "messages")
        or _parts_problem(
            body.get("messages"),
            "messages",
            ("content", "tool_calls"),
            noun="content parts and tool calls",
        )
        or _length_problem(body.get("tools"), MAX_TOOLS, "tools")
        or _length_problem(body.get("stop"), MAX_STOP_SEQUENCES, "stop sequences")
    )


def messages_limit_problem(body: dict[str, Any]) -> str | None:
    """Describe the first count in an Anthropic Messages body over its limit, or None.

    Counts ``messages``, the content blocks of each (with the blocks nested in
    a ``tool_result``), the ``system`` blocks, ``tools`` and
    ``stop_sequences``.
    """
    system = body.get("system")
    return (
        _length_problem(body.get("messages"), MAX_MESSAGES, "messages")
        or _length_problem(system, MAX_PARTS_PER_MESSAGE, "system blocks")
        or _parts_problem(
            body.get("messages"),
            "messages",
            ("content",),
            noun="content blocks",
            nested=True,
            already=len(system) if isinstance(system, list) else 0,
        )
        or _length_problem(body.get("tools"), MAX_TOOLS, "tools")
        or _length_problem(body.get("stop_sequences"), MAX_STOP_SEQUENCES, "stop sequences")
    )


def responses_limit_problem(body: dict[str, Any]) -> str | None:
    """Describe the first count in a Responses body over its limit, or None.

    Counts ``input`` items (each becomes a message, or a tool call), the parts
    of each item's ``content`` or ``output``, and ``tools``. Checked before
    the request is translated, which walks every item; the translated request
    is checked again as a chat request, with any stored conversation it
    continues.
    """
    return (
        _length_problem(body.get("input"), MAX_MESSAGES, "input items")
        or _parts_problem(body.get("input"), "input", ("content", "output"), noun="content parts")
        or _length_problem(body.get("tools"), MAX_TOOLS, "tools")
    )


def _length_problem(value: Any, limit: int, what: str) -> str | None:
    if isinstance(value, list) and len(value) > limit:
        return f"Too many {what}: {len(value)}, at most {limit}"
    return None


def _parts_problem(
    items: Any,
    field: str,
    keys: tuple[str, ...],
    *,
    noun: str,
    nested: bool = False,
    already: int = 0,
) -> str | None:
    """Check the parts in each item's *keys* lists, per item and in all.

    *nested* also counts a ``content`` list inside a part (an Anthropic
    ``tool_result``'s blocks). *already* is how many parts the request holds
    outside *items*. Each list's length is added before the list is walked,
    and the walk stops at the first count over its limit.
    """
    if not isinstance(items, list):
        return None
    total = already
    for index, item in enumerate(items):
        count = _item_parts(item, keys, nested=nested) if isinstance(item, dict) else 0
        if count > MAX_PARTS_PER_MESSAGE:
            return f"Too many {noun} in {field}[{index}]: more than {MAX_PARTS_PER_MESSAGE}"
        total += count
        if total > MAX_PARTS_PER_REQUEST:
            return f"Too many {noun}: more than {MAX_PARTS_PER_REQUEST} in all"
    return None


def _item_parts(item: dict[str, Any], keys: tuple[str, ...], *, nested: bool) -> int:
    """How many parts *item* holds, counted up to just past ``MAX_PARTS_PER_MESSAGE``."""
    count = 0
    for key in keys:
        parts = item.get(key)
        if not isinstance(parts, list):
            continue
        count += len(parts)
        if count > MAX_PARTS_PER_MESSAGE:
            return count
        if not nested:
            continue
        for part in parts:
            inner = part.get("content") if isinstance(part, dict) else None
            if isinstance(inner, list):
                count += len(inner)
                if count > MAX_PARTS_PER_MESSAGE:
                    return count
    return count
