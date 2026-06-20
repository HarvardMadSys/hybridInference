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
    ],
)
def test_rejects_generic_or_missing_openers(content: str) -> None:
    assert agent_name_from_prompt([{"role": "system", "content": content}]) is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # A known agent named anywhere in the first sentence is matched, even
        # when it is not the subject of a "You are <Name>" opener.
        ("The user is Claude.", "Claude"),
        ("Defer to Cursor for edits.", "Cursor"),
        ("This session runs under Codex.", "Codex"),
    ],
)
def test_matches_common_agent_anywhere_in_first_sentence(content: str, expected: str) -> None:
    assert agent_name_from_prompt([{"role": "system", "content": content}]) == expected


def test_reads_developer_role() -> None:
    prompt = [{"role": "developer", "content": "You are Codex, a coding agent."}]
    assert agent_name_from_prompt(prompt) == "Codex"


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
        # Case-insensitive keyword match.
        ("you are OPENCLAW running in VS Code.", "openClaw"),
        # Keyword need not follow "You are" — it can appear anywhere in the
        # opening sentence.
        ("You are an expert coding assistant operating inside pi", "pi"),
    ],
)
def test_recognizes_client_keywords(content: str, expected: str) -> None:
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
