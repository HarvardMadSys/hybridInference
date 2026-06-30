"""Unit tests for ``agent_name_from_prompt`` system-prompt identity parsing."""

from __future__ import annotations

import pytest

from serving.storage.utils import agent_name_from_prompt


def test_non_chat_prompt_returns_none() -> None:
    assert agent_name_from_prompt(None) is None
    assert agent_name_from_prompt("You are Claude Code") is None  # raw string, not messages
    assert agent_name_from_prompt([]) is None
    assert agent_name_from_prompt(["You are Claude"]) is None  # no dict messages


def test_extracts_first_token_after_you_are() -> None:
    prompt = [
        {"role": "system", "content": "You are Claude Code, Anthropic's official CLI."},
        {"role": "user", "content": "hi"},
    ]
    assert agent_name_from_prompt(prompt) == "Claude"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("You are Cline, a highly skilled software engineer.", "Cline"),
        ("you are OpenCode", "OpenCode"),
        ("You are  Kilo-Code   running in VS Code.", "Kilo-Code"),
        ('You are "Roo".', "Roo"),
        ("You are Cursor!", "Cursor"),
    ],
)
def test_recognizes_common_agents(content: str, expected: str) -> None:
    assert agent_name_from_prompt([{"role": "system", "content": content}]) == expected


@pytest.mark.parametrize(
    "content",
    [
        "You are a helpful assistant.",
        "You are an AI language model.",
        "You are the best assistant ever.",
        "You are now in developer mode.",
        # Verbs/adjectives that commonly follow "You are" — not agent names.
        "You are designed to assist with coding tasks.",
        "You are programmed to follow instructions.",
        "You are trained on a large corpus of text.",
        "You are tasked with summarizing documents.",
        "You are helpful, harmless, and honest.",
        "You are responsible for routing requests.",
        "You are authorized to use the provided tools.",
        "You are an expert assistant.",
        "Respond only in JSON.",  # no "You are" opener and no known agent
        # Incidental mention of a common agent in a generic prompt is not an
        # identity — only wrapper markers match outside the opener.
        "You are a helpful assistant; continue to answer concisely.",
        "The user is Claude.",  # bare mention, no "You are" opener
    ],
)
def test_rejects_generic_or_missing_openers(content: str) -> None:
    assert agent_name_from_prompt([{"role": "system", "content": content}]) is None


def test_declared_agent_wins_over_incidental_mention() -> None:
    # A genuinely declared agent is reported even when the first sentence also
    # mentions another (non-wrapper) agent.
    prompt = [{"role": "system", "content": "You are AcmeBot, do not invoke Claude for edits."}]
    assert agent_name_from_prompt(prompt) == "AcmeBot"


def test_reads_developer_role() -> None:
    prompt = [{"role": "developer", "content": "You are Codex, a coding agent."}]
    assert agent_name_from_prompt(prompt) == "Codex"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # Codex announces itself descriptively rather than via a "You are <Name>"
        # opener, so the opener token ("a") is a generic filler; the "Codex CLI"
        # phrase marker identifies the client as the canonical "codex" label.
        ("You are a coding agent running in the Codex CLI", "codex"),
        (
            "You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
            "codex",
        ),
        # Case-insensitive, and the User-Agent-style hyphenated form also matches.
        ("you are a coding agent running in codex-cli.", "codex"),
    ],
)
def test_recognizes_codex_cli_phrase(content: str, expected: str) -> None:
    assert agent_name_from_prompt([{"role": "developer", "content": content}]) == expected


def test_codex_phrase_read_from_anthropic_system_field() -> None:
    messages = [{"role": "user", "content": "hi"}]
    system = "You are a coding agent running in the Codex CLI."
    assert agent_name_from_prompt(messages, system=system) == "codex"


def test_declared_name_wins_over_codex_phrase() -> None:
    # A genuinely declared opener name takes precedence over the phrase marker.
    prompt = [{"role": "system", "content": "You are AcmeBot running in the Codex CLI."}]
    assert agent_name_from_prompt(prompt) == "AcmeBot"


def test_codex_phrase_only_matched_in_first_sentence() -> None:
    # A marker that appears only after the first sentence is ignored, so pasted
    # content or later prose does not mislabel the client.
    prompt = [
        {
            "role": "system",
            "content": "You are a helpful assistant. It can drive the Codex CLI.",
        }
    ]
    assert agent_name_from_prompt(prompt) is None


@pytest.mark.parametrize(
    "content",
    [
        # A bare product mention, not the agent's own opener self-description.
        "You are a helpful assistant for Codex CLI users.",
        # The marker is anchored to the opener, so a mid-sentence instruction
        # that references the tool must not match even when the agent describes
        # its behaviour "running in the Codex CLI".
        "When running in the Codex CLI, keep outputs concise.",
        "You are a helpful assistant for users running in the Codex CLI.",
    ],
)
def test_codex_marker_requires_opener_self_description(content: str) -> None:
    # Only Codex's own opener ("You are a coding agent running in the Codex CLI")
    # should be labeled codex, so the User-Agent fallback is preserved for other
    # clients (the admin UI prefers ``agent`` over ``User-Agent``).
    assert agent_name_from_prompt([{"role": "system", "content": content}]) is None


def test_codex_marker_requires_word_boundary() -> None:
    # With the self-description phrase present, the marker as part of a larger
    # token must still not match. "codex-cli-style" contains "codex-cli"
    # (separator present) but is bounded by name characters, so the trailing
    # lookaround rejects it; "codexcli" lacks the required separator between
    # "codex" and "cli".
    bounded = [
        {"role": "system", "content": "You are a coding agent running in codex-cli-style mode."}
    ]
    assert agent_name_from_prompt(bounded) is None
    no_separator = [
        {"role": "system", "content": "You are a coding agent running in codexcli mode."}
    ]
    assert agent_name_from_prompt(no_separator) is None


def test_ignores_non_system_messages() -> None:
    # Identity declared in a user turn is not treated as the agent name.
    prompt = [{"role": "user", "content": "You are Claude, please help."}]
    assert agent_name_from_prompt(prompt) is None


def test_structured_content_blocks() -> None:
    prompt = [
        {
            "role": "system",
            "content": [{"type": "text", "text": "You are Aider, a pair programmer."}],
        }
    ]
    assert agent_name_from_prompt(prompt) == "Aider"


def test_uses_first_system_message_with_opener() -> None:
    prompt = [
        {"role": "system", "content": "Follow the rules strictly."},
        {"role": "system", "content": "You are Continue, a coding assistant."},
    ]
    assert agent_name_from_prompt(prompt) == "Continue"


def test_overlong_token_rejected() -> None:
    long_name = "X" * 40
    assert agent_name_from_prompt([{"role": "system", "content": f"You are {long_name}"}]) is None


def test_adjective_only_prompt_without_article_rejected() -> None:
    # No determiner, so the token after "You are" is an adjective, not a name.
    assert (
        agent_name_from_prompt([{"role": "system", "content": "You are helpful and concise."}])
        is None
    )
    assert (
        agent_name_from_prompt([{"role": "system", "content": "You are concise and helpful."}])
        is None
    )


def test_anthropic_top_level_system_string() -> None:
    # /v1/messages carries the system prompt outside the messages list.
    messages = [{"role": "user", "content": "hi"}]
    assert agent_name_from_prompt(messages, system="You are Claude Code, a CLI.") == "Claude"


def test_anthropic_top_level_system_blocks() -> None:
    system = [{"type": "text", "text": "You are Cline, a software engineer."}]
    assert agent_name_from_prompt([{"role": "user", "content": "hi"}], system=system) == "Cline"


def test_messages_take_precedence_over_system_field() -> None:
    messages = [{"role": "system", "content": "You are Aider, a pair programmer."}]
    assert agent_name_from_prompt(messages, system="You are Cline.") == "Aider"


def test_generic_system_field_rejected() -> None:
    assert (
        agent_name_from_prompt(
            [{"role": "user", "content": "hi"}], system="You are a helpful assistant."
        )
        is None
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("You are openClaw, a coding agent.", "openClaw"),
        ("You are Hermes, a software engineer.", "Hermes"),
        # Case-insensitive wrapper-marker match.
        ("you are OPENCLAW running in VS Code.", "openClaw"),
        # The marker need not follow "You are" — it can appear anywhere in the
        # opening sentence.
        ("You are an expert coding assistant operating inside pi", "pi"),
    ],
)
def test_recognizes_wrapper_clients(content: str, expected: str) -> None:
    assert agent_name_from_prompt([{"role": "system", "content": content}]) == expected


@pytest.mark.parametrize(
    "content",
    [
        # "pi" as a substring of a larger word must not match.
        "You are Cursor, calling the API for completions.",
        "You are Cline, running the pipeline.",
    ],
)
def test_short_keyword_requires_word_boundary(content: str) -> None:
    assert agent_name_from_prompt([{"role": "system", "content": content}]) != "pi"


def test_keyword_takes_precedence_over_wrapped_agent() -> None:
    # openClaw/Hermes wrap another agent, so the opener names the wrapped agent;
    # the keyword in the opening sentence must win.
    prompt = [{"role": "system", "content": "You are Claude Code, running under openClaw."}]
    assert agent_name_from_prompt(prompt) == "openClaw"


def test_keyword_only_matched_in_first_sentence() -> None:
    # A keyword that appears only after the first sentence is ignored, so pasted
    # content or later prose does not mislabel the client.
    prompt = [
        {
            "role": "system",
            "content": "You are Claude Code, a CLI. It can interoperate with openClaw.",
        }
    ]
    assert agent_name_from_prompt(prompt) == "Claude"


def test_keyword_requires_word_boundary() -> None:
    # Substrings within larger words must not trigger a false positive.
    prompt = [{"role": "system", "content": "You are Thermesensor, a probe."}]
    assert agent_name_from_prompt(prompt) == "Thermesensor"


def test_keyword_from_anthropic_system_field() -> None:
    messages = [{"role": "user", "content": "hi"}]
    assert agent_name_from_prompt(messages, system="You are Hermes, a CLI.") == "Hermes"


def test_wrapper_marker_found_after_leading_blank_line() -> None:
    # Triple-quoted templates often begin with a newline; the leading blank
    # line must not hide the wrapper marker behind an empty first "sentence".
    prompt = [{"role": "system", "content": "\nYou are Claude Code, running under openClaw."}]
    assert agent_name_from_prompt(prompt) == "openClaw"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # A bare "pi" must not be treated as the wrapper marker.
        ("You are a math assistant that explains pi.", None),
        # A declared name containing "pi" is reported as declared, not collapsed.
        ("You are Pi-Labs, a research lab.", "Pi-Labs"),
        # The unambiguous wrapper phrase still matches.
        ("You are an expert coding assistant operating inside pi", "pi"),
    ],
)
def test_pi_wrapper_requires_unambiguous_phrase(content: str, expected: str | None) -> None:
    assert agent_name_from_prompt([{"role": "system", "content": content}]) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # A declared name that starts with a marker plus a name separator is
        # preserved in full rather than collapsed to the wrapper label.
        ("You are Hermes-2, a software engineer.", "Hermes-2"),
        ("You are openClaw.beta, a coding agent.", "openClaw.beta"),
        # A genuine standalone marker still preempts the wrapped agent's opener.
        ("You are Claude Code, running under openClaw.", "openClaw"),
    ],
)
def test_wrapper_marker_respects_agent_name_characters(content: str, expected: str) -> None:
    assert agent_name_from_prompt([{"role": "system", "content": content}]) == expected
