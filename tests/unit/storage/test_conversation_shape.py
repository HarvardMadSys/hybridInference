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
    assert num_user_turns == 2
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
