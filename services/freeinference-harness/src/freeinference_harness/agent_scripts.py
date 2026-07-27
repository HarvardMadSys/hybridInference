"""Deterministic agent-loop scripts shared by the fake provider and checkers.

Single source of truth for the agent-loop conformance layer (issue #1041,
P-1 layer 1). The fake provider replays these scripts verbatim, and the
agent-loop scenario executors assert against the same definitions, so the
two sides can never drift apart.

Marker protocol: the driver embeds ``[[agent-script:<id>]]`` inside a user
message. The fake provider selects the script from that marker and picks the
scripted turn by counting assistant messages in the incoming request, which
keeps the server fully stateless across turns of one conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MARKER_PREFIX = "[[agent-script:"
MARKER_SUFFIX = "]]"

# Exact malformed fragment from the DeepSeek-V4/SGLang incident: one stream
# emitted these bytes as tool-call arguments and the client echoed them into
# every later request of the session (see
# tests/unit/adapters/test_anthropic_translator.py in the main repository).
DS4_MALFORMED_ARGUMENTS = '{}""'


def marker(script_id: str) -> str:
    """Returns the user-message marker that selects a script."""
    return f"{MARKER_PREFIX}{script_id}{MARKER_SUFFIX}"


def extract_script_id(text: str) -> str | None:
    """Extracts a script id from marker text, or returns None."""
    start = text.find(MARKER_PREFIX)
    if start < 0:
        return None
    end = text.find(MARKER_SUFFIX, start + len(MARKER_PREFIX))
    if end < 0:
        return None
    return text[start + len(MARKER_PREFIX) : end].strip() or None


def find_script_id(messages: list[dict[str, Any]]) -> str | None:
    """Finds the script marker in the most recent user message that has one.

    Handles both string content and content-part lists so the marker survives
    every gateway surface translation.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        for text in texts:
            script_id = extract_script_id(text)
            if script_id:
                return script_id
    return None


def count_assistant_turns(messages: list[dict[str, Any]]) -> int:
    """Counts assistant messages, which indexes the scripted turn to serve."""
    return sum(1 for message in messages if message.get("role") == "assistant")


@dataclass(frozen=True)
class ScriptTurn:
    """One scripted provider turn.

    ``kind`` selects the replay behavior:

    - ``text``: assistant text answer, finish_reason ``stop``.
    - ``tool_call``: one tool call whose arguments are emitted as the exact
      ``argument_fragments`` sequence (one SSE delta per fragment).
    - ``empty``: terminates cleanly with no visible content or tool calls.
    - ``disconnect``: emits ``text_fragments`` then closes the connection
      without a finish chunk or ``[DONE]``.
    """

    kind: str
    text: str = ""
    tool_name: str = ""
    argument_fragments: tuple[str, ...] = ()
    text_fragments: tuple[str, ...] = ()
    status_first_attempt: int | None = None

    @property
    def joined_arguments(self) -> str:
        """Returns the fully spliced tool-call arguments string."""
        return "".join(self.argument_fragments)


@dataclass(frozen=True)
class Expected:
    """Normalized expectations checked by the scenario executors."""

    final_text_contains: str = ""
    tool_name: str = ""
    tool_arguments_raw: str | None = None
    tool_input_object: dict[str, Any] | None = field(default=None)
    max_steps: int = 4
    retry_on_429: bool = False
    expect_truncated_stream: bool = False
    expect_empty_final: bool = False


@dataclass(frozen=True)
class AgentScript:
    """A deterministic multi-turn provider script plus its expectations."""

    script_id: str
    turns: tuple[ScriptTurn, ...]
    expected: Expected

    def turn_for(self, assistant_count: int) -> ScriptTurn:
        """Returns the scripted turn for a given assistant-message count."""
        index = min(assistant_count, len(self.turns) - 1)
        return self.turns[index]


_BASH = "bash"

SCRIPTS: dict[str, AgentScript] = {
    "basic_tool_roundtrip": AgentScript(
        script_id="basic_tool_roundtrip",
        turns=(
            ScriptTurn(
                kind="tool_call",
                tool_name=_BASH,
                argument_fragments=('{"command": "pwd"}',),
            ),
            ScriptTurn(kind="text", text="PWD_OK: agent loop round trip complete."),
        ),
        expected=Expected(
            final_text_contains="PWD_OK",
            tool_name=_BASH,
            tool_arguments_raw='{"command": "pwd"}',
            tool_input_object={"command": "pwd"},
        ),
    ),
    "fragmented_args": AgentScript(
        script_id="fragmented_args",
        turns=(
            ScriptTurn(
                kind="tool_call",
                tool_name=_BASH,
                argument_fragments=('{"comm', 'and": "up', 'time"}'),
            ),
            ScriptTurn(kind="text", text="UPTIME_OK: fragments spliced correctly."),
        ),
        expected=Expected(
            final_text_contains="UPTIME_OK",
            tool_name=_BASH,
            tool_arguments_raw='{"command": "uptime"}',
            tool_input_object={"command": "uptime"},
        ),
    ),
    "ds4_malformed_args": AgentScript(
        script_id="ds4_malformed_args",
        turns=(
            ScriptTurn(
                kind="tool_call",
                tool_name=_BASH,
                # Split exactly like the broken upstream streaming parser did.
                argument_fragments=("{}", '""'),
            ),
            ScriptTurn(kind="text", text="RECOVERED_OK: session survived malformed arguments."),
        ),
        expected=Expected(
            final_text_contains="RECOVERED_OK",
            tool_name=_BASH,
            # OpenAI surface passes the malformed string through verbatim ...
            tool_arguments_raw=DS4_MALFORMED_ARGUMENTS,
            # ... while the Anthropic surface must normalize it to an object.
            tool_input_object={},
        ),
    ),
    "empty_content": AgentScript(
        script_id="empty_content",
        turns=(ScriptTurn(kind="empty"),),
        expected=Expected(max_steps=1, expect_empty_final=True),
    ),
    "rate_limited_then_ok": AgentScript(
        script_id="rate_limited_then_ok",
        turns=(
            ScriptTurn(
                kind="text",
                text="AFTER_429_OK: retry after rate limit succeeded.",
                status_first_attempt=429,
            ),
        ),
        expected=Expected(
            final_text_contains="AFTER_429_OK",
            max_steps=1,
            retry_on_429=True,
        ),
    ),
    "midstream_disconnect": AgentScript(
        script_id="midstream_disconnect",
        turns=(
            ScriptTurn(
                kind="disconnect",
                text_fragments=("PARTIAL_", "STREAM_"),
            ),
        ),
        expected=Expected(max_steps=1, expect_truncated_stream=True),
    ),
    "cancel_mid_stream": AgentScript(
        script_id="cancel_mid_stream",
        turns=(
            ScriptTurn(
                kind="text",
                text_fragments=("CANCEL_", "CHUNK_1_", "CHUNK_2_", "CHUNK_3_", "CHUNK_4_"),
            ),
            ScriptTurn(kind="text", text="AFTER_CANCEL_OK: server survived a client abort."),
        ),
        expected=Expected(final_text_contains="AFTER_CANCEL_OK", max_steps=2),
    ),
    "runtime_smoke": AgentScript(
        script_id="runtime_smoke",
        turns=(
            ScriptTurn(
                kind="text",
                text="RUNTIME_SMOKE_OK: the runtime-to-gateway-to-fake chain works.",
            ),
        ),
        expected=Expected(final_text_contains="RUNTIME_SMOKE_OK", max_steps=1),
    ),
    "poisoned_history": AgentScript(
        script_id="poisoned_history",
        turns=(
            ScriptTurn(kind="text", text="UNUSED: poisoned-history scripts start at turn 1."),
            ScriptTurn(kind="text", text="POISON_HANDLED_OK: poisoned history was normalized."),
        ),
        expected=Expected(final_text_contains="POISON_HANDLED_OK", max_steps=1),
    ),
}


def get_script(script_id: str) -> AgentScript:
    """Returns a script by id, raising a clear error for unknown ids."""
    try:
        return SCRIPTS[script_id]
    except KeyError as exc:
        known = ", ".join(sorted(SCRIPTS))
        raise KeyError(f"Unknown agent script '{script_id}'. Known scripts: {known}") from exc
