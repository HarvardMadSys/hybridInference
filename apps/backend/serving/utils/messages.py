"""Shape normalization for OpenAI-style chat message lists.

Pure functions, no I/O. Shared by the northbound translators (which build
message lists) and by the OpenAI-compatible adapter (which is the last stop
before a message list leaves the gateway), so a rule that every
OpenAI-compatible upstream cares about only has to be written once.
"""

from __future__ import annotations

from typing import Any

__all__ = ["merge_leading_system_messages"]


def _system_text(content: Any) -> str:
    """Flatten a chat message's ``content`` (str or content-block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
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
    merged_text = "\n\n".join(t for t in (_system_text(m.get("content")) for m in systems) if t)
    leading = dict(systems[0])
    leading["content"] = merged_text
    return [leading, *others]
