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
        "Respond only in JSON.",  # no "You are" opener
        "The user is Claude.",  # opener not at start
    ],
)
def test_rejects_generic_or_missing_openers(content: str) -> None:
    assert agent_name_from_prompt([{"role": "system", "content": content}]) is None


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
