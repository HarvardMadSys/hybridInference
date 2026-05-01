"""Unit tests for Azure OpenAI message ordering profile.

Azure OpenAI / OpenAI requires that tool messages immediately follow the assistant
message that contains tool_calls. Some clients (e.g., Codex CLI) may insert extra
assistant "preamble" messages or user messages between tool_calls and the tool response.
"""

from serving.adapters.profiles import ProviderProfile, normalize_messages_for_profile


def _normalize(messages: list[dict]) -> list[dict]:
    """Normalize messages using the Azure OpenAI provider profile."""
    normalized = normalize_messages_for_profile(ProviderProfile.AZURE_OPENAI, messages)
    assert normalized is not None
    return normalized


def _get_roles(messages: list[dict]) -> list[str]:
    """Extract roles from messages for easy comparison."""
    return [m.get("role") for m in messages]


def _assert_tool_follows_tool_calls(messages: list[dict]) -> None:
    """Assert that tool messages immediately follow tool_calls messages."""
    for i, msg in enumerate(messages[:-1]):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            next_msg = messages[i + 1]
            assert next_msg.get("role") == "tool", (
                f"tool message must immediately follow assistant tool_calls message, "
                f"but got {next_msg.get('role')}"
            )


class TestMergeAssistantPreambles:
    """Tests for merging consecutive assistant messages."""

    def test_merges_single_preamble(self) -> None:
        """Merges assistant preamble into assistant tool_calls message."""
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "shell", "arguments": "{}"},
                    }
                ],
            },
            {"role": "assistant", "content": "I'll run the tool now."},
            {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
        ]

        fixed = _normalize(messages)

        assert _get_roles(fixed) == ["user", "assistant", "tool"]
        assert fixed[1].get("tool_calls"), "Merged assistant must preserve tool_calls"
        assert fixed[1].get("content") == "I'll run the tool now."
        _assert_tool_follows_tool_calls(fixed)

    def test_merges_multiple_preambles(self) -> None:
        """Merges multiple consecutive assistant preambles."""
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                    }
                ],
            },
            {"role": "assistant", "content": "First preamble"},
            {"role": "assistant", "content": "Second preamble"},
            {"role": "tool", "content": "result", "tool_call_id": "call_1"},
        ]

        fixed = _normalize(messages)

        assert _get_roles(fixed) == ["assistant", "tool"]
        assert fixed[0].get("tool_calls")
        assert fixed[0].get("content") == "First preamble\nSecond preamble"
        _assert_tool_follows_tool_calls(fixed)


class TestReorderToolMessages:
    """Tests for reordering tool messages to follow their corresponding tool_calls."""

    def test_reorders_tool_after_user(self) -> None:
        """Reorders tool message when user message appears between tool_calls and tool."""
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "shell"}}],
            },
            {"role": "user", "content": "By the way..."},
            {"role": "tool", "content": "result", "tool_call_id": "call_1"},
        ]

        fixed = _normalize(messages)

        assert _get_roles(fixed) == ["assistant", "tool", "user"]
        _assert_tool_follows_tool_calls(fixed)

    def test_reorders_tool_after_preamble_and_user(self) -> None:
        """Handles both preamble merge and tool reordering."""
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "shell"}}],
            },
            {"role": "assistant", "content": "Let me check..."},
            {"role": "user", "content": "Also check the README"},
            {"role": "tool", "content": "file1.txt", "tool_call_id": "call_1"},
        ]

        fixed = _normalize(messages)

        assert _get_roles(fixed) == ["assistant", "tool", "user"]
        assert fixed[0].get("content") == "Let me check..."
        assert fixed[0].get("tool_calls")
        _assert_tool_follows_tool_calls(fixed)

    def test_handles_multiple_tool_calls(self) -> None:
        """Correctly reorders multiple tool messages."""
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "ls"}},
                    {"id": "call_2", "type": "function", "function": {"name": "cat"}},
                ],
            },
            {"role": "user", "content": "Interruption"},
            {"role": "tool", "content": "result1", "tool_call_id": "call_1"},
            {"role": "tool", "content": "result2", "tool_call_id": "call_2"},
        ]

        fixed = _normalize(messages)

        assert _get_roles(fixed) == ["assistant", "tool", "tool", "user"]
        assert fixed[1].get("tool_call_id") == "call_1"
        assert fixed[2].get("tool_call_id") == "call_2"
        _assert_tool_follows_tool_calls(fixed)


class TestNoOpCases:
    """Tests for cases where no reordering is needed."""

    def test_valid_sequence_unchanged(self) -> None:
        """Leaves already-valid sequences unchanged."""
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "shell"}}],
            },
            {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "Done."},
        ]

        fixed = _normalize(messages)

        assert fixed == messages
        _assert_tool_follows_tool_calls(fixed)

    def test_no_tool_calls(self) -> None:
        """Handles messages without any tool calls."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        fixed = _normalize(messages)

        assert fixed == messages
