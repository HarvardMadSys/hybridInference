"""Helpers for extracting human-readable content out of stored request payloads.

The gateway logs each request's raw JSON body to ``api_logs.request_payload``.
These helpers pull the *content* out of those payloads — the opening of the
system prompt (which usually identifies the calling agent/harness — Kilo Code,
opencode, Claude Code, "pi", etc.) and the user-turn text — so we can answer
*what* a user is prompting, not just how much traffic they generate.

Both OpenAI-shape (``messages``) and Anthropic-shape (``system`` + content
blocks) payloads are handled. The functions are deliberately pure and
dependency-light so they can be shared by the admin Usage Insights endpoint
(``serving``) and the offline analysis scripts (``ops/db/analysis``).

**Where the messages live.** The conversation turns are stored once, in the
dedicated ``api_logs.prompt`` column (TEXT holding ``json.dumps(messages)``);
the storage layer strips ``messages`` out of ``request_payload`` before insert
so the same bytes are not written twice. Rows logged before that change still
carry a copy inside the payload, so every extractor here takes the ``prompt``
column as an optional argument, prefers it, and falls back to
``request_payload["messages"]`` for those historical rows. ``system`` is *not*
stripped and is still read straight off the payload.
"""

from __future__ import annotations

import json
from typing import Any


def as_payload_dict(payload: Any) -> dict[str, Any]:
    """Coerce a stored payload (dict or JSON string) into a dict.

    Returns an empty dict when the payload is missing or cannot be parsed.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def as_message_list(prompt: Any) -> list[Any]:
    """Coerce a stored ``api_logs.prompt`` value into a list of messages.

    The column is TEXT holding ``json.dumps(messages)``, but callers may already
    hold a decoded list. Anything else — NULL, a bare string, malformed JSON —
    yields an empty list, which makes callers fall back to ``request_payload``.
    """
    if isinstance(prompt, (bytes, bytearray)):
        prompt = prompt.decode("utf-8", "replace")
    if isinstance(prompt, str):
        try:
            prompt = json.loads(prompt)
        except (json.JSONDecodeError, ValueError):
            return []
    return prompt if isinstance(prompt, list) else []


def messages_of(payload: dict[str, Any], prompt: Any = None) -> list[Any]:
    """Return a request's message list, preferring the dedicated ``prompt`` column.

    ``prompt`` is the source of truth: the storage layer no longer duplicates
    ``messages`` into ``request_payload``. Rows written before that change kept a
    copy in the payload, so fall back to it when the column is empty/absent.
    """
    msgs = as_message_list(prompt)
    if msgs:
        return msgs
    raw = payload.get("messages")
    return raw if isinstance(raw, list) else []


def system_opener(payload: dict[str, Any], max_chars: int, prompt: Any = None) -> str | None:
    """Return the start of the system prompt, or None if absent.

    Handles the Anthropic ``system`` field (string or list of content blocks)
    and the OpenAI ``role: system`` message (string or list of text blocks).
    Pass the row's ``prompt`` column so the OpenAI system turn is still found on
    rows whose payload no longer carries ``messages``.
    """
    sysval = payload.get("system")
    if isinstance(sysval, str) and sysval.strip():
        return sysval.strip()[:max_chars]
    if isinstance(sysval, list):
        parts = [b.get("text", "") for b in sysval if isinstance(b, dict)]
        joined = "\n".join(p for p in parts if p).strip()
        if joined:
            return joined[:max_chars]
    for msg in messages_of(payload, prompt):
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()[:max_chars]
            if isinstance(content, list):
                parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n".join(p for p in parts if p).strip()
                if joined:
                    return joined[:max_chars]
    return None


def user_messages(payload: dict[str, Any], max_chars: int, prompt: Any = None) -> list[str]:
    """Return user-turn text out of a request (OpenAI or Anthropic shape).

    Each returned string is truncated to ``max_chars``. Empty turns are skipped.
    Pass the row's ``prompt`` column: it is where the turns actually live now,
    with ``request_payload["messages"]`` kept only as the historical fallback.
    """
    out: list[str] = []
    for msg in messages_of(payload, prompt):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = "\n".join(p for p in parts if p)
        else:
            text = json.dumps(content, default=str)
        text = (text or "").strip()
        if text:
            out.append(text[:max_chars])
    return out


def user_agent_from_metadata(metadata: Any) -> str | None:
    """Pull the ``user_agent`` out of a stored metadata blob (dict or JSON string)."""
    md = metadata
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except json.JSONDecodeError:
            md = {}
    return md.get("user_agent") if isinstance(md, dict) else None


def referer_from_metadata(metadata: Any) -> str | None:
    """Pull the ``referer`` out of a stored metadata blob (dict or JSON string)."""
    md = metadata
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except json.JSONDecodeError:
            md = {}
    return md.get("referer") if isinstance(md, dict) else None
