"""Shape normalization for OpenAI-style chat message lists.

Pure functions, no I/O. Shared by the northbound translators (which build
message lists) and by the OpenAI-compatible adapter (which is the last stop
before a message list leaves the gateway), so a rule that every
OpenAI-compatible upstream cares about only has to be written once.
"""

from __future__ import annotations

from typing import Any

__all__ = ["flatten_text_content", "merge_leading_system_messages", "move_tool_media"]

#: Content part types that carry media rather than text: the non-text keys of
#: ``serving.schemas.CONTENT_BLOCK_MODALITY``. Not imported from there, since
#: that module imports this one; a test keeps the two in step.
MEDIA_PART_TYPES = frozenset(
    {
        "image_url",
        "image",
        "input_image",
        "input_audio",
        "audio",
        "video_url",
        "video",
        "input_video",
    }
)
_IMAGE_PART_TYPES = frozenset({"image_url", "image", "input_image"})


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


def move_tool_media(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Move the media in ``role: "tool"`` messages to the user message after their run.

    A tool message holds text only: OpenAI's Chat Completions refuses an image
    part there, and the providers that follow it refuse it or drop it. But a
    tool can return one -- Codex's ``view_image`` arrives through the Responses
    surface as a ``function_call_output`` with an ``input_image`` part, and a
    chat client can send one directly. So each such tool message keeps its
    text and says its media follow, and the media go, introduced by the call
    they came from, after the whole run of tool messages: the tool messages
    answering one assistant turn must stay contiguous. They join the user
    message that follows the run, ahead of its own content, or else a new
    user message, so the conversation keeps one user message per turn. The
    Anthropic surface places a ``tool_result``'s images the same way when it
    translates (``anthropic_translator``).

    Returns the argument itself (no copy) when no tool message carries media,
    which is nearly all traffic. Linear in the parts: each is looked at once
    to sort it into text and media.
    """
    if not any(_has_media(m) for m in messages if m.get("role") == "tool"):
        return messages
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool":
            kept, media = _split_media(message)
            if media:
                message, label = _tool_text_only(message, kept, media)
                pending.extend([label, *media])
            out.append(message)
            continue
        if pending:
            if message.get("role") == "user":
                message = {**message, "content": [*pending, *_as_parts(message.get("content"))]}
            else:
                out.append({"role": "user", "content": pending})
            pending = []
        out.append(message)
    if pending:
        out.append({"role": "user", "content": pending})
    return out


def _as_parts(content: Any) -> list[Any]:
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        return [content]
    if isinstance(content, str) and content:
        return [{"type": "text", "text": content}]
    return []


def _is_media(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    kind = part.get("type")
    return isinstance(kind, str) and kind in MEDIA_PART_TYPES


def _has_media(message: dict[str, Any]) -> bool:
    return any(_is_media(part) for part in _as_parts(message.get("content")))


def _split_media(message: dict[str, Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """A message's content parts other than media, and its media, in their order.

    Each part goes to one side by its own type, in one pass. Removing the media
    from the parts by value (``part not in media``) was quadratic: 5,000 text
    and 5,000 image parts took 1.6 s on the event loop, on every attempt.
    """
    kept: list[Any] = []
    media: list[dict[str, Any]] = []
    for part in _as_parts(message.get("content")):
        (media if _is_media(part) else kept).append(part)
    return kept, media


def _tool_text_only(
    message: dict[str, Any], kept: list[Any], media: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The tool message with only its *kept* parts, and the label introducing its *media*."""
    text = flatten_text_content(kept)
    images = all(part.get("type") in _IMAGE_PART_TYPES for part in media)
    noun, article = ("image", "an") if images else ("media part", "a")
    counted = f"{article} {noun}" if len(media) == 1 else f"{len(media)} {noun}s"
    note = f"[The tool returned {counted}; attached in the next user message.]"
    stripped = {**message, "content": f"{text}\n{note}" if text else note}
    call_id = message.get("tool_call_id", "")
    title = "Image output" if images else "Output"
    return stripped, {"type": "text", "text": f"{title} of tool call {call_id}:"}
