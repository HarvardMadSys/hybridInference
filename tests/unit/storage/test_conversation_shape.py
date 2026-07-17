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
    # Plain-string user content the human typed (OpenAI shape) is a genuine turn.
    assert conversation_shape([{"role": "user", "content": "hello"}]) == (1, 1, 0)


def test_system_reminder_only_user_message_not_counted() -> None:
    # Claude Code injects context as user-role <system-reminder> messages. They
    # are not turns the human typed, so they must not count.
    prompt = [
        {"role": "user", "content": "<system-reminder>\nSkills available: ...\n</system-reminder>"},
        {"role": "user", "content": "clone the repo with gh cli"},
    ]
    assert conversation_shape(prompt) == (2, 1, 0)


def test_user_text_with_appended_system_reminder_counts() -> None:
    # A genuine turn with a <system-reminder> appended still counts: the human
    # typed the leading text.
    prompt = [
        {
            "role": "user",
            "content": "make the rotation faster\n<system-reminder>context</system-reminder>",
        },
    ]
    assert conversation_shape(prompt) == (1, 1, 0)


def test_context_usage_preamble_before_user_text_counts() -> None:
    # Some harnesses prefix a <context_usage> preamble; the human text after it
    # makes the message a genuine turn.
    prompt = [
        {
            "role": "user",
            "content": "<context_usage>41% used</context_usage>\nnow fix the header",
        },
    ]
    assert conversation_shape(prompt) == (1, 1, 0)


def test_injected_wrappers_and_notices_not_counted() -> None:
    # A spread of injected user-role messages coding agents emit, none of which
    # the human typed.
    injected = [
        "<task-notification>\nbackground task done\n</task-notification>",
        "<environment_details>\ncwd=/x\n</environment_details>",
        "<local-command-stdout>ok</local-command-stdout>",
        "[System: You edited code in this turn, but the workspace does not have ...]",
        '[IMPORTANT: The user has invoked the "airtable" skill, follow it ...]',
        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.",
    ]
    prompt = [{"role": "user", "content": c} for c in injected]
    num_turns, num_user_turns, num_tool_calls = conversation_shape(prompt)
    assert num_turns == len(injected)
    assert num_user_turns == 0
    assert num_tool_calls == 0


def test_queued_user_message_in_system_reminder_counts() -> None:
    # Claude Code wraps a message the user queued mid-turn inside a
    # <system-reminder> with this exact marker -- it is a genuine human turn.
    wrapped = (
        "<system-reminder>\nThe user sent the following message:\n\n"
        "always re-read the full file\n\nPlease address this message.\n</system-reminder>"
    )
    assert conversation_shape([{"role": "user", "content": wrapped}]) == (1, 1, 0)


def test_genuine_user_wrapper_tags_counted() -> None:
    # Tags that wrap the human's own words (not harness injection) still count.
    prompt = [
        {"role": "user", "content": "<user_interjection>wait, stop</user_interjection>"},
        {"role": "user", "content": "<user_message>go ahead</user_message>"},
    ]
    assert conversation_shape(prompt) == (2, 2, 0)


def test_image_only_user_message_counts() -> None:
    # A user message with an image (or other attachment) and no text is still a
    # genuine turn -- the human sent the image.
    prompt = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:..."}}]},
    ]
    assert conversation_shape(prompt) == (1, 1, 0)


def test_content_less_user_message_not_counted() -> None:
    # A user-role message with no content is degenerate, not a turn the human
    # took, so it does not count (num_turns still counts the message).
    assert conversation_shape([{"role": "user"}]) == (1, 0, 0)
