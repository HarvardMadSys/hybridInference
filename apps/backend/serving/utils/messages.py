"""Shape normalization for OpenAI-style chat message lists.

Pure functions, no I/O. Shared by the northbound translators (which build
message lists) and by the OpenAI-compatible adapter (which is the last stop
before a message list leaves the gateway), so a rule that every
OpenAI-compatible upstream cares about only has to be written once.
"""

from __future__ import annotations

from typing import Any

__all__ = ["flatten_text_content", "merge_leading_system_messages"]


def flatten_text_content(content: Any) -> str:
    """Flatten a chat message's ``content`` to plain text.

    The northbound ``content`` field is deliberately permissive (``Any``), so
    every shape a client can legally send has to survive: a plain string, a
    block list, a bare block mapping that was never wrapped in a list, and a
    list mixing plain strings with blocks. A block contributes whatever string
    sits under its ``text`` key regardless of its declared ``type`` -- clients
    label text blocks ``text``, ``input_text`` and ``output_text``, and
    dropping one over its label loses real instructions.

    This is the single flattener for the OpenAI-compatible path:
    :func:`merge_leading_system_messages` uses it to combine system prompts and
    ``openai_compat._normalize_text_content`` uses it to flatten content for a
    text-only model. They handled overlapping shapes differently once, and a
    system prompt that one accepted came out empty from the other.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def merge_leading_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse every ``system`` message into a single leading one.

    Some backends reject a request unless at most one message has role
    ``system`` and it is first — sglang and vLLM, the local inference servers
    this gateway is built around, answer anything else with
    ``System message must be at the beginning`` and a 400, failing the whole
    turn. Two callers produce a list that trips it:

    - a Responses-API request carrying both the top-level ``instructions``
      field and a "developer" item folded to ``system`` (see
      :func:`~serving.responses_translator.responses_input_to_messages`) —
      both read as system-level context but land as separate entries;
    - any client posting straight to ``/v1/chat/completions`` with several
      system messages, or one left mid-transcript.

    Merge their text and drop the duplicates, preserving the relative order of
    everything else. Returns the argument itself (no copy) when the list is
    already in the accepted shape, which is the overwhelming majority of
    traffic.
    """
    systems = [m for m in messages if m.get("role") == "system"]
    if len(systems) <= 1 and (not systems or messages[0].get("role") == "system"):
        return messages
    others = [m for m in messages if m.get("role") != "system"]
    if len(systems) == 1:
        return [systems[0], *others]
    merged_text = "\n\n".join(
        t for t in (flatten_text_content(m.get("content")) for m in systems) if t
    )
    leading = dict(systems[0])
    leading["content"] = merged_text
    return [leading, *others]
