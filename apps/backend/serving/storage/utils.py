"""Pure utility functions for the storage layer.

These functions have no database dependencies and can be imported safely
without constructing storage clients.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
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


def _is_genuine_user_turn(message: dict[str, Any]) -> bool:
    """Return True when a user-role message carries real user input.

    Anthropic-shape tool results (Claude Code) arrive as user-role messages
    whose ``content`` is a list of ``tool_result`` blocks. Those are the
    conversation's tool responses, not turns the human typed, so they must not
    inflate ``num_user_turns``. A user message counts as a genuine turn when its
    content is a plain string (or otherwise non-list), or a block list that
    contains at least one element that is not a ``tool_result`` block. A content
    list made up solely of ``tool_result`` blocks is a tool response and does
    not count.
    """
    content = message.get("content")
    if not isinstance(content, list):
        # Plain string / None content: an ordinary user message.
        return True
    has_tool_result = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            has_tool_result = True
        else:
            # Any non-tool_result element means the human contributed input.
            return True
    # All blocks (if any) were tool_result. An empty content list carried no
    # tool result, so it is still a (degenerate) user turn.
    return not has_tool_result


def conversation_shape(
    prompt: list[dict[str, Any]] | str | None,
) -> tuple[int | None, int | None, int | None]:
    """Derive ``(num_turns, num_user_turns, num_tool_calls)`` from a prompt.

    Computed once at log time so the admin list query can read three cheap
    integer columns instead of de-TOASTing the full request payload per row.
    ``num_turns`` counts all messages, ``num_user_turns`` counts user-role
    messages that carry real user input, and ``num_tool_calls`` sums tool calls
    across messages. Anthropic-shape tool results (user-role messages whose
    content is entirely ``tool_result`` blocks) are excluded from
    ``num_user_turns`` because they are tool responses, not human turns. Both the
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
        if message.get("role") == "user" and _is_genuine_user_turn(message):
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


def _shannon_entropy(text: str) -> float:
    """Return the Shannon entropy (bits per character) of ``text``.

    ``0.0`` for empty text. A single repeated character is ``0.0``; varied
    natural-language text runs ~4 bits/char, so a low value flags structured or
    repetitive machine-generated content.
    """
    if not text:
        return 0.0
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in Counter(text).values())


def user_message_stats(
    prompt: list[dict[str, Any]] | str | None,
) -> tuple[int | None, float | None, int | None]:
    """Derive ``(chars, entropy, hash)`` for the latest user message in a prompt.

    Computed once at log time (like :func:`conversation_shape`) so the automation
    score can read cheap columns instead of de-TOASTing the full payload per row.
    The metrics describe the **newest user-role message** -- the actual new input
    that request carries, since each chat request resends the prior history:

    * ``chars`` -- character length of the flattened message text;
    * ``entropy`` -- Shannon entropy (bits/char) of that text;
    * ``hash`` -- a stable signed 64-bit BLAKE2b hash of the stripped text, used
      to detect a user resending the same/near-identical message across requests.

    Returns ``(None, None, None)`` when ``prompt`` is not a chat-style messages
    list or carries no non-empty user message (e.g. embeddings, raw completions).
    """
    if not isinstance(prompt, list):
        return None, None, None
    text: str | None = None
    for message in reversed(prompt):
        if isinstance(message, dict) and message.get("role") == "user":
            text = _message_text(message.get("content"))
            break
    if not text:
        return None, None, None
    normalized = text.strip()
    if not normalized:
        return None, None, None
    digest = hashlib.blake2b(normalized.encode("utf-8", "ignore"), digest_size=8).digest()
    return len(normalized), _shannon_entropy(normalized), int.from_bytes(digest, "big", signed=True)


def _first_sentence(text: str) -> str:
    """Return the leading sentence of ``text`` (up to the first sentence end or newline).

    Leading whitespace is ignored first, so a prompt that begins with a blank
    line (common in triple-quoted templates) does not yield an empty sentence.
    A ``.`` ends the sentence only when followed by whitespace or end-of-text,
    so an intra-name dot (e.g. ``"openClaw.beta"``) does not split the name.
    """
    text = text.lstrip()
    match = re.search(r"[!?\n]|\.(?=\s|$)", text)
    return text[: match.start()] if match else text


# Agent-name characters (per ``_AGENT_NAME_RE``); used to bound wrapper markers
# so a declared name that merely starts with a marker (e.g. ``"Hermes-2"``,
# ``"openClaw.beta"``) is not collapsed to the wrapper label.
_NAME_CHARS = "A-Za-z0-9._-"


def _wrapper_client_from_first_sentence(text: str) -> str | None:
    r"""Return a wrapper client's canonical name if its marker is in the first sentence.

    Wrapper clients (openClaw/Hermes/pi) name themselves by a distinctive marker
    in the opening sentence and typically embed the opener of the agent they
    wrap (e.g. ``"You are Claude Code, running under openClaw"``), so they are
    matched ahead of the ``"You are <Name>"`` opener and reported instead of the
    wrapped agent. Only these unambiguous markers are matched here — common
    agents are left to the opener so an incidental mention does not override a
    genuinely declared identity. Matching is case-insensitive and scoped to the
    opening sentence, so a marker deeper in a long prompt does not match.

    Markers are bounded by ``_NAME_CHARS`` rather than ``\b`` so a declared name
    that merely starts with a marker followed by a name separator — e.g.
    ``"Hermes-2"`` or ``"openClaw.beta"`` — is left to the opener and preserved
    in full. ``pi`` is additionally too short and ambiguous for a bare match (it
    would catch ``"explains pi"``), so it requires the wrapper phrase
    ``"inside pi"``.
    """
    sentence = _first_sentence(text).lower()

    def has(marker: str) -> bool:
        pattern = rf"(?<![{_NAME_CHARS}]){re.escape(marker)}(?![{_NAME_CHARS}])"
        return re.search(pattern, sentence) is not None

    if has("openclaw"):
        return "openClaw"
    if has("hermes"):
        return "Hermes"
    if re.search(rf"(?<![{_NAME_CHARS}])inside\s+pi(?![{_NAME_CHARS}])", sentence):
        return "pi"
    return None


# Clients that announce themselves with a descriptive opener rather than a
# ``"You are <Name>"`` opener, so ``_name_from_opener`` yields a generic filler.
# Each marker maps to the canonical client label used by the frontend
# ``parseClientTool`` (e.g. ``codex-cli/…`` → ``"codex"``), so the admin UI shows
# the same name whether the client is identified by its system prompt or its
# ``User-Agent``. Each pattern is anchored (``^``) to the client's own opener
# self-description, not merely an occurrence of the phrase: an unanchored match
# would label any prompt that *mentions* the tool — e.g. ``"You are a helpful
# assistant for Codex CLI users"`` or the instruction ``"When running in the
# Codex CLI, keep outputs concise"`` — as the client, and since the admin UI
# prefers ``agent`` over ``User-Agent`` that would suppress the User-Agent
# fallback and corrupt other clients' labels. The trailing ``_NAME_CHARS``
# lookaround keeps the marker from matching inside a larger token, and matching
# is case-insensitive. ``Codex`` opens with ``"You are a coding agent running in
# the Codex CLI"``, whose first token after ``"You are"`` is the filler ``"a"``.
_PHRASE_CLIENT_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"^\s*you\s+are\s+a\s+coding\s+agent\s+running\s+in\s+"
            rf"(?:the\s+)?codex[\s_-]+cli(?![{_NAME_CHARS}])",
            re.IGNORECASE,
        ),
        "codex",
    ),
)


def _phrase_client_from_first_sentence(text: str) -> str | None:
    """Return a client label when a known opener self-description starts the first sentence.

    A fallback for clients whose opener does not declare a name (see
    ``_PHRASE_CLIENT_MARKERS``). Each marker is anchored to the start of the
    opening sentence, so only the client's own opener matches — an incidental
    mention of the tool later in the sentence (or in pasted content) does not.
    Consulted only after the ``"You are <Name>"`` opener yields no usable name,
    so a genuinely declared identity still wins.
    """
    sentence = _first_sentence(text)
    for pattern, name in _PHRASE_CLIENT_MARKERS:
        if pattern.search(sentence):
            return name
    return None


def _name_from_opener(text: str) -> str | None:
    """Return the name declared by a ``"You are <Name>"`` opener, or None.

    Only the first token after ``"You are"`` is taken, it must look like a name
    (leading letter; letters/digits/``.-_``; at most 32 chars) and must not be a
    generic filler such as ``"a"``/``"the"`` or a common role verb/adjective
    such as ``"helpful"``/``"designed"``.
    """
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


def _agent_name_from_text(text: str | None) -> str | None:
    """Return the agent name for a system prompt, or None.

    A wrapper client named in the opening sentence (see
    ``_wrapper_client_from_first_sentence``) takes precedence, since those
    clients wrap another agent and would otherwise be mislabeled by the generic
    opener. Otherwise the name declared by a ``"You are <Name>"`` opener is used
    (see ``_name_from_opener``). When the opener yields no usable name, a known
    phrase marker in the opening sentence (see
    ``_phrase_client_from_first_sentence``) is used as a fallback — covering
    clients such as Codex that announce themselves descriptively
    (``"You are a coding agent running in the Codex CLI"``) rather than by a
    ``"You are <Name>"`` opener.
    """
    if not isinstance(text, str):
        return None
    wrapper_name = _wrapper_client_from_first_sentence(text)
    if wrapper_name is not None:
        return wrapper_name
    name = _name_from_opener(text)
    if name is not None:
        return name
    return _phrase_client_from_first_sentence(text)


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

    Wrapper clients (e.g. ``openClaw``/``Hermes``/``pi``) carry the opener of
    the agent they wrap, so a distinctive wrapper marker in the opening sentence
    (see ``_wrapper_client_from_first_sentence``) is matched first and reported
    as the wrapper rather than the wrapped agent. All other agents are taken
    from the ``"You are <Name>"`` opener, so an incidental mention of an agent
    does not override a genuinely declared identity. Clients that announce
    themselves descriptively instead of by a ``"You are <Name>"`` opener — e.g.
    Codex (``"You are a coding agent running in the Codex CLI"``) — are matched
    by a phrase marker in the opening sentence only after the opener yields no
    usable name, and reported by their canonical label (``"codex"``).

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


def billable_output_tokens(
    *,
    prompt_tokens: float,
    completion_tokens: float,
    reasoning_tokens: float = 0.0,
    total_tokens: float | None = None,
) -> float:
    """Return output tokens to bill at the completion price.

    Providers are not consistent about whether ``completion_tokens`` already
    includes reasoning. When ``total_tokens`` proves it does, avoid charging the
    separately reported ``reasoning_tokens`` twice. When total usage is absent
    or ambiguous, preserve the historical conservative behavior and bill
    completion plus reasoning.
    """
    if reasoning_tokens <= 0:
        return completion_tokens

    if total_tokens is not None:
        prompt_plus_completion = prompt_tokens + completion_tokens
        if math.isclose(total_tokens, prompt_plus_completion, rel_tol=0.0, abs_tol=1e-9):
            return completion_tokens

        prompt_plus_completion_plus_reasoning = prompt_plus_completion + reasoning_tokens
        if math.isclose(
            total_tokens,
            prompt_plus_completion_plus_reasoning,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            return completion_tokens + reasoning_tokens

    return completion_tokens + reasoning_tokens


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
        total_raw = usage.get("total_tokens")
        total_tokens = float(total_raw) if total_raw is not None else None
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

        output_tokens = billable_output_tokens(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
        )

        return (
            (billable_prompt_tokens * prompt_price / 1_000_000)
            + (output_tokens * completion_price / 1_000_000)
            + (cache_read_tokens * cache_read_price / 1_000_000)
            + (cache_write_tokens * cache_write_price / 1_000_000)
        )
    except (ValueError, TypeError):
        return None
