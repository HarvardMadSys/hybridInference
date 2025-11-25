"""Unit tests for Claude adapter message merging.

Tests that consecutive messages with the same role are properly merged,
as required by Claude API's alternating user/assistant role constraint.
"""

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.claude import ClaudeAdapter


@pytest.fixture
def claude_adapter():
    """Create a Claude adapter instance for testing."""
    config = ModelConfig(
        id="claude-test",
        name="Claude Test",
        provider="claude",
        base_url="https://test.example.com",
        api_key="test-key",
        context_length=200000,
        max_output_length=4096,
        supports_tools=True,
    )
    return ClaudeAdapter(config)


def test_merge_consecutive_assistant_messages(claude_adapter):
    """Test that consecutive assistant messages are merged into one."""
    # Simulate OpenAI format with consecutive assistant messages
    # This can happen when tool_calls are in a separate message
    messages = [
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                }
            ],
        },
        {"role": "assistant", "content": "Let me check the weather."},
        {"role": "tool", "content": "Sunny, 72F", "tool_call_id": "call_1"},
        {"role": "user", "content": "Thanks"},
    ]

    converted = claude_adapter._convert_messages(messages)

    # Verify no consecutive assistant messages
    for i in range(len(converted) - 1):
        assert not (
            converted[i]["role"] == "assistant" and converted[i + 1]["role"] == "assistant"
        ), f"Found consecutive assistant messages at index {i}"

    # Verify the assistant message contains both tool_use and text
    assistant_msgs = [m for m in converted if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1, "Should have exactly one assistant message"

    assistant_content = assistant_msgs[0]["content"]
    block_types = [b["type"] for b in assistant_content]
    assert "tool_use" in block_types, "Should contain tool_use block"
    assert "text" in block_types, "Should contain text block"


def test_merge_consecutive_user_messages(claude_adapter):
    """Test that consecutive user messages are merged into one."""
    messages = [
        {"role": "user", "content": "First message"},
        {"role": "user", "content": "Second message"},
        {"role": "assistant", "content": "Response"},
    ]

    converted = claude_adapter._convert_messages(messages)

    # Verify no consecutive user messages
    for i in range(len(converted) - 1):
        assert not (
            converted[i]["role"] == "user" and converted[i + 1]["role"] == "user"
        ), f"Found consecutive user messages at index {i}"

    # Verify the user message contains both text blocks
    user_msgs = [m for m in converted if m["role"] == "user"]
    assert len(user_msgs) == 1, "Should have exactly one user message"
    assert len(user_msgs[0]["content"]) == 2, "Should have two text blocks"


def test_merge_tool_results_with_user_messages(claude_adapter):
    """Test that tool results are merged with preceding user messages."""
    messages = [
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                }
            ],
        },
        {"role": "tool", "content": "Sunny, 72F", "tool_call_id": "call_1"},
        {"role": "user", "content": "What about tomorrow?"},
    ]

    converted = claude_adapter._convert_messages(messages)

    # Find the user message that should contain the tool_result
    user_msgs = [m for m in converted if m["role"] == "user"]
    # Should have 2 user messages: initial "Hello" and merged tool_result + "What about tomorrow?"
    assert len(user_msgs) == 2, f"Expected 2 user messages, got {len(user_msgs)}"

    # The second user message should contain both tool_result and text
    second_user = user_msgs[1]
    block_types = [b["type"] for b in second_user["content"]]
    assert "tool_result" in block_types, "Should contain tool_result block"
    assert "text" in block_types, "Should contain text block"


def test_alternating_roles(claude_adapter):
    """Test that the final message list has strictly alternating roles."""
    messages = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Message 1"},
        {"role": "user", "content": "Message 2"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "tool1", "arguments": "{}"},
                }
            ],
        },
        {"role": "assistant", "content": "Thinking..."},
        {"role": "tool", "content": "Result", "tool_call_id": "call_1"},
        {"role": "user", "content": "Follow up"},
    ]

    converted = claude_adapter._convert_messages(messages)

    # Verify strictly alternating roles (system is skipped)
    for i in range(len(converted) - 1):
        assert (
            converted[i]["role"] != converted[i + 1]["role"]
        ), f"Non-alternating roles at index {i}: {converted[i]['role']} -> {converted[i+1]['role']}"


def test_empty_assistant_messages_are_skipped(claude_adapter):
    """Test that consecutive assistant messages are merged, even if some are empty."""
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": ""},  # Empty, filtered by strip() check
        {"role": "assistant", "content": "   "},  # Whitespace only, filtered by strip() check
        {"role": "assistant", "content": "Real response"},
    ]

    converted = claude_adapter._convert_messages(messages)

    assistant_msgs = [m for m in converted if m["role"] == "assistant"]
    # All assistant messages should be merged into one
    assert len(assistant_msgs) == 1, "Should have only one merged assistant message"

    # The merged message should contain text blocks
    content_blocks = assistant_msgs[0]["content"]
    text_blocks = [b for b in content_blocks if b.get("type") == "text"]
    # Empty strings are filtered by strip(), so we should have at least the real response
    assert len(text_blocks) >= 1, f"Expected at least 1 text block, got {len(text_blocks)}"

    # Find the real response in the text blocks
    real_response_found = any(b["text"] == "Real response" for b in text_blocks)
    assert real_response_found, "Should contain the real response"


def test_text_before_tool_use_in_assistant_messages(claude_adapter):
    """Test that text blocks come before tool_use blocks in assistant messages.

    This is required by Claude API - text must precede tool_use blocks.
    """
    messages = [
        {"role": "user", "content": "What's the weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                }
            ],
        },
        {"role": "assistant", "content": "Let me check the weather for you."},
    ]

    converted = claude_adapter._convert_messages(messages)

    assistant_msgs = [m for m in converted if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1, "Should have one merged assistant message"

    content = assistant_msgs[0]["content"]

    # Find indices of text and tool_use blocks
    text_indices = [i for i, b in enumerate(content) if b.get("type") == "text"]
    tool_indices = [i for i, b in enumerate(content) if b.get("type") == "tool_use"]

    assert len(text_indices) > 0, "Should have text blocks"
    assert len(tool_indices) > 0, "Should have tool_use blocks"

    # All text blocks should come before all tool_use blocks
    max_text_index = max(text_indices)
    min_tool_index = min(tool_indices)

    assert max_text_index < min_tool_index, (
        f"Text blocks must come before tool_use blocks. "
        f"Last text at index {max_text_index}, first tool at {min_tool_index}"
    )
