"""Unit tests for ``conversation_shape`` turn/tool-call derivation."""

from __future__ import annotations

from serving.storage.utils import conversation_shape


def test_non_chat_prompt_returns_none() -> None:
    assert conversation_shape("just a string") == (None, None, None)
    assert conversation_shape(None) == (None, None, None)
    # A list with no dict-shaped (message-like) elements is not a chat prompt.
    assert conversation_shape(["a", "b"]) == (None, None, None)


def test_openai_tool_calls_counted() -> None:
    prompt = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "what's the weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "function": {"name": "get_weather"}},
                {"id": "call_2", "function": {"name": "get_time"}},
            ],
        },
    ]
    assert conversation_shape(prompt) == (3, 1, 2)


def test_anthropic_tool_use_blocks_counted() -> None:
    # Claude Code carries tool calls as ``tool_use`` content blocks and tool
    # results as ``tool_result`` blocks inside user-role messages.
    prompt = [
        {"role": "user", "content": [{"type": "text", "text": "list files"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll list them."},
                {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"cmd": "ls"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "a.txt"},
            ],
        },
    ]
    num_turns, num_user_turns, num_tool_calls = conversation_shape(prompt)
    assert num_turns == 3
    # The final user-role message carries only a ``tool_result`` block: it is a
    # tool response, not a turn the human typed, so it does not count toward
    # ``num_user_turns``. Only the genuine "list files" user message does.
    assert num_user_turns == 1
    assert num_tool_calls == 1


def test_mixed_shapes_counted_together() -> None:
    prompt = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "tu_2", "name": "Grep", "input": {}},
            ],
            "tool_calls": [{"id": "call_1", "function": {"name": "legacy"}}],
        },
    ]
    assert conversation_shape(prompt) == (1, 0, 3)


def test_anthropic_tool_result_user_messages_not_counted() -> None:
    # A Claude Code agentic loop: one genuine user turn, several assistant
    # tool_use turns, and several tool_result carrier messages that arrive with
    # role="user". Only the genuine user message counts toward num_user_turns.
    prompt = [
        {"role": "user", "content": [{"type": "text", "text": "fix the bug"}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "..."}],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_2", "name": "Edit", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_2", "content": "ok"}],
        },
    ]
    num_turns, num_user_turns, num_tool_calls = conversation_shape(prompt)
    assert num_turns == 5
    assert num_user_turns == 1
    assert num_tool_calls == 2


def test_user_message_mixing_text_and_tool_result_counts() -> None:
    # A user message that carries a tool_result AND real text still counts: the
    # human contributed input that turn.
    prompt = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "done"},
                {"type": "text", "text": "now also update the docs"},
            ],
        },
    ]
    assert conversation_shape(prompt) == (1, 1, 0)


def test_string_content_user_message_counts() -> None:
    # Plain-string user content (OpenAI shape) is always a genuine user turn.
    assert conversation_shape([{"role": "user", "content": "hello"}]) == (1, 1, 0)
