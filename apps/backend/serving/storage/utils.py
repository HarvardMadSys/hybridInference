"""Pure utility functions for the storage layer.

These functions have no database dependencies and can be imported safely
without constructing storage clients.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any


def json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats with ``None`` for JSON storage."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    return value


def conversation_shape(
    prompt: list[dict[str, Any]] | str | None,
) -> tuple[int | None, int | None, int | None]:
    """Derive ``(num_turns, num_user_turns, num_tool_calls)`` from a prompt.

    Computed once at log time so the admin list query can read three cheap
    integer columns instead of de-TOASTing the full request payload per row.
    ``num_turns`` counts all messages, ``num_user_turns`` counts user-role
    messages, and ``num_tool_calls`` sums tool calls across messages. Both the
    OpenAI shape (an assistant ``tool_calls`` array) and the Anthropic Messages
    shape used by Claude Code (``tool_use`` content blocks) are counted, so the
    column is accurate regardless of which API surface the request came in on.

    Returns ``(None, None, None)`` when ``prompt`` is not a chat-style messages
    list — e.g. a raw completion string, or an embedding input such as a list
    of strings/token-id arrays. Only dict-shaped (message-like) elements are
    counted, and a list with none of them is treated as non-chat so the admin
    UI shows ``—`` rather than a misleading zero-turn conversation.
    """
    if not isinstance(prompt, list):
        return None, None, None
    num_turns = 0
    num_user_turns = 0
    num_tool_calls = 0
    for message in prompt:
        if not isinstance(message, dict):
            continue
        num_turns += 1
        if message.get("role") == "user":
            num_user_turns += 1
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            num_tool_calls += len(tool_calls)
        # Anthropic Messages format (e.g. Claude Code) carries tool calls as
        # ``tool_use`` content blocks rather than an OpenAI ``tool_calls`` array.
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    num_tool_calls += 1
    if num_turns == 0:
        return None, None, None
    return num_turns, num_user_turns, num_tool_calls


# Leading "You are <token>" opener, captured from the start of a system prompt.
_YOU_ARE_RE = re.compile(r"^\s*you\s+are\s+(?P<name>\S+)", re.IGNORECASE)
# Known coding-agent clients that identify themselves by a keyword in the
# opening sentence of the system prompt rather than via a "You are <Name>"
# opener. Many wrap another agent (e.g. Claude Code), so the generic opener
# would mislabel them as the wrapped agent — a keyword match takes precedence.
# Each entry maps a lowercase keyword to the canonical client name to report.
_CLIENT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("openclaw", "openClaw"),
    ("hermes", "Hermes"),
    ("pi", "pi"),
)
# A plausible agent name: starts with a letter, then letters/digits/.-_ only.
_AGENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
# Generic fillers that follow "You are" in non-agent prompts (e.g. "You are a
# helpful assistant"). These are rejected so the column carries real agent
# identities rather than noise.
_GENERIC_AGENT_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "my",
        "your",
        "our",
        "his",
        "her",
        "its",
        "their",
        "this",
        "that",
        "one",
        "no",
        "not",
        "only",
        "just",
        "also",
        "now",
        "here",
        "currently",
        "being",
        "going",
        "to",
        "in",
        "on",
        "at",
        "about",
        "very",
        "really",
        "always",
        "never",
        # Verbs/adjectives that commonly follow "You are" in generic prompts
        # ("You are designed to ...", "You are a helpful assistant"). None are
        # plausible agent names, so filtering them avoids false positives.
        "designed",
        "programmed",
        "trained",
        "built",
        "created",
        "developed",
        "made",
        "powered",
        "tasked",
        "meant",
        "supposed",
        "expected",
        "required",
        "allowed",
        "able",
        "capable",
        "responsible",
        "running",
        "working",
        "operating",
        "acting",
        "helping",
        "assisting",
        "chatting",
        "talking",
        "interacting",
        "part",
        "helpful",
        "harmless",
        "honest",
        "friendly",
        "knowledgeable",
        "free",
        "welcome",
        "encouraged",
        "instructed",
        "authorized",
        "permitted",
        "forbidden",
        "prohibited",
        "representing",
        "professional",
        "specialized",
        "expert",
        "assistant",
        "concise",
        "accurate",
        "precise",
        "thorough",
        "polite",
        "patient",
        "reliable",
        "efficient",
        "smart",
        "intelligent",
    }
)


def _message_text(content: Any) -> str | None:
    """Flatten a message ``content`` field to plain text, or None if empty.

    Handles both the plain-string form and the structured content-block form
    (OpenAI/Anthropic), concatenating the textual blocks.
    """
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        joined = " ".join(parts).strip()
        return joined or None
    return None


def _first_sentence(text: str) -> str:
    """Return the leading sentence of ``text`` (up to the first ``.!?`` or newline)."""
    match = re.search(r"[.!?\n]", text)
    return text[: match.start()] if match else text


def _client_from_keywords(text: str) -> str | None:
    """Return a known client name if its keyword appears in the first sentence.

    Matches each ``_CLIENT_KEYWORDS`` entry case-insensitively on a word
    boundary within the opening sentence only, so a keyword buried deeper in a
    long prompt (or pasted user content) does not produce a false positive.
    """
    sentence = _first_sentence(text).lower()
    for keyword, canonical in _CLIENT_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", sentence):
            return canonical
    return None


def _agent_name_from_text(text: str | None) -> str | None:
    """Return the agent name for a system prompt, or None.

    A known client keyword in the opening sentence (see ``_CLIENT_KEYWORDS``)
    takes precedence, since those clients often wrap another agent and would
    otherwise be mislabeled by the generic opener. Otherwise the name declared
    by a ``"You are <Name>"`` opener is used, subject to the shared guardrails:
    only the first token after ``"You are"`` is taken, it must look like a name
    (leading letter; letters/digits/``.-_``; at most 32 chars) and must not be a
    generic filler such as ``"a"``/``"the"`` or a common role verb/adjective
    such as ``"helpful"``/``"designed"``.
    """
    if not isinstance(text, str):
        return None
    keyword_name = _client_from_keywords(text)
    if keyword_name is not None:
        return keyword_name
    match = _YOU_ARE_RE.match(text)
    if match is None:
        return None
    name = match.group("name").strip("\"'`*.,;:!?()[]{}<>")
    if not name or len(name) > 32:
        return None
    if name.lower() in _GENERIC_AGENT_WORDS:
        return None
    if not _AGENT_NAME_RE.match(name):
        return None
    return name


def agent_name_from_prompt(
    prompt: list[dict[str, Any]] | str | None,
    system: Any = None,
) -> str | None:
    """Extract the calling agent's self-declared name from a system prompt.

    Several coding agents announce themselves in the opening of their system
    prompt — e.g. ``"You are Claude Code, ..."`` or ``"You are Cline, ..."``.
    When that pattern is present in a system (or ``developer``) message, the
    leading token after ``"You are"`` is returned so the admin dashboard can
    label the client by its declared identity instead of the ``User-Agent``
    header. Returns ``None`` when no system prompt carries a recognizable
    opener, so callers fall back to User-Agent parsing.

    Some clients (see ``_CLIENT_KEYWORDS``, e.g. ``openClaw``/``Hermes``) wrap
    another agent and so carry the wrapped agent's opener. For those, a known
    keyword anywhere in the opening sentence of the system prompt takes
    precedence over the opener, so the wrapper is reported rather than the
    agent it wraps.

    ``system`` is the optional top-level system field used by the Anthropic
    ``/v1/messages`` surface (Claude Code), where the system prompt is carried
    outside the ``messages`` list as a string or a list of text blocks. It is
    consulted only when the ``messages`` themselves yield no name.

    Guardrails: only the first token after ``"You are"`` is taken, it must look
    like a name (leading letter; letters/digits/``.-_``; at most 32 chars) and
    must not be a generic filler such as ``"a"``/``"the"``/``"your"`` or a
    common role word such as ``"helpful"`` — so a prompt like ``"You are a
    helpful assistant"`` yields ``None`` rather than ``"a"``.
    """
    if isinstance(prompt, list):
        for message in prompt:
            if not isinstance(message, dict):
                continue
            if message.get("role") not in ("system", "developer"):
                continue
            name = _agent_name_from_text(_message_text(message.get("content")))
            if name is not None:
                return name
    if system is not None:
        return _agent_name_from_text(_message_text(system))
    return None


def strip_null_bytes(value: Any) -> Any:
    """Recursively remove PostgreSQL-incompatible null bytes from strings."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {
            strip_null_bytes(k) if isinstance(k, str) else k: strip_null_bytes(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [strip_null_bytes(v) for v in value]
    if isinstance(value, tuple):
        return [strip_null_bytes(v) for v in value]
    return value


def coerce_json_object(value: Any) -> dict[str, Any] | None:
    """Return a JSON object from decoded JSON/JSONB values, or None for non-objects."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def calculate_cost(
    usage: dict[str, Any] | None,
    pricing: dict[str, str] | None,
) -> float | None:
    """Compute request cost in USD based on usage and pricing tables.

    Uses OpenAI semantics: ``prompt_tokens`` is the *total* input including
    any cached portion. The cached subset is reported separately in
    ``cache_read_tokens`` / ``cache_write_tokens`` and billed at its own
    rate, so we subtract it from ``prompt_tokens`` before applying
    ``prompt_price`` to avoid double-charging. Cached tokens are only
    subtracted when a specific cache price is configured (>0); otherwise
    they fall back to being billed at the regular prompt rate so models
    that report cache tokens but lack cache-specific pricing aren't
    silently under-billed.
    """
    if not usage or not pricing:
        return None

    try:
        prompt_tokens = float(usage.get("prompt_tokens", 0))
        completion_tokens = float(usage.get("completion_tokens", 0))
        reasoning_tokens = float(usage.get("reasoning_tokens", 0))
        cache_read_tokens = float(usage.get("cache_read_tokens", 0))
        cache_write_tokens = float(usage.get("cache_write_tokens", 0))

        prompt_price = float(pricing.get("prompt", "0"))
        completion_price = float(pricing.get("completion", "0"))
        cache_read_price = float(pricing.get("input_cache_reads", "0"))
        cache_write_price = float(pricing.get("input_cache_writes", "0"))

        billable_prompt_tokens = prompt_tokens
        if cache_read_price > 0:
            billable_prompt_tokens -= cache_read_tokens
        if cache_write_price > 0:
            billable_prompt_tokens -= cache_write_tokens
        billable_prompt_tokens = max(billable_prompt_tokens, 0.0)

        return (
            (billable_prompt_tokens * prompt_price / 1_000_000)
            + (completion_tokens * completion_price / 1_000_000)
            + (reasoning_tokens * completion_price / 1_000_000)
            + (cache_read_tokens * cache_read_price / 1_000_000)
            + (cache_write_tokens * cache_write_price / 1_000_000)
        )
    except (ValueError, TypeError):
        return None
