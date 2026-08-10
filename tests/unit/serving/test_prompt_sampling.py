"""Tests for the shared prompt/system extractors in ``serving.utils.prompt_sampling``.

The storage layer stores each request's conversation turns once, in the dedicated
``api_logs.prompt`` column, and strips ``messages`` out of ``request_payload``
before insert. Rows written before that change still carry the copy inside the
payload. Every consumer of these helpers therefore has to read *both* shapes, so
these tests pin the fallback in both directions: an old-shape row (messages only
in the payload) and a new-shape row (messages only in the prompt column).
"""

from __future__ import annotations

import json

from serving.utils.prompt_sampling import (
    as_message_list,
    as_payload_dict,
    messages_of,
    system_opener,
    user_messages,
)

# --- row fixtures -----------------------------------------------------------

_TURNS = [
    {"role": "system", "content": "You are Claude Code, an agentic CLI."},
    {"role": "user", "content": "Fix the flaky test in test_router.py"},
    {"role": "assistant", "content": "Looking at it now."},
    {"role": "user", "content": "Also update the changelog"},
]

# Pre-dedup row: request_payload carries the whole body, prompt column unused here.
OLD_SHAPE_PAYLOAD = {"model": "glm-5.1", "stream": True, "messages": _TURNS}

# Post-dedup row: the payload keeps only the call parameters (and, on the
# Anthropic surface, ``system``); the turns live in the prompt column.
NEW_SHAPE_PAYLOAD = {"model": "glm-5.1", "stream": True}
NEW_SHAPE_PROMPT = json.dumps(_TURNS)  # the column is TEXT holding json.dumps(...)


class TestAsMessageList:
    def test_decodes_the_text_column(self):
        assert as_message_list(json.dumps(_TURNS)) == _TURNS

    def test_accepts_an_already_decoded_list(self):
        assert as_message_list(_TURNS) == _TURNS

    def test_non_list_values_yield_empty_so_callers_fall_back(self):
        # NULL column, a bare string (str(prompt) for a non-list prompt),
        # malformed JSON, and a JSON object are all "no turns here".
        for value in (None, "", "not json {", json.dumps({"messages": _TURNS}), 17):
            assert as_message_list(value) == []


class TestMessagesOf:
    def test_prefers_the_prompt_column(self):
        """When both are present the column wins — it is the source of truth."""
        stale = [{"role": "user", "content": "an older copy"}]
        assert messages_of({"messages": stale}, json.dumps(_TURNS)) == _TURNS

    def test_falls_back_to_the_payload_for_historical_rows(self):
        assert messages_of(OLD_SHAPE_PAYLOAD, None) == _TURNS

    def test_empty_prompt_column_falls_back_rather_than_returning_nothing(self):
        assert messages_of(OLD_SHAPE_PAYLOAD, "") == _TURNS

    def test_no_messages_anywhere(self):
        assert messages_of(NEW_SHAPE_PAYLOAD, None) == []

    def test_non_list_payload_messages_are_ignored(self):
        assert messages_of({"messages": "oops"}, None) == []


class TestSystemOpener:
    def test_openai_system_turn_old_shape(self):
        assert system_opener(OLD_SHAPE_PAYLOAD, 200) == "You are Claude Code, an agentic CLI."

    def test_openai_system_turn_new_shape(self):
        """The system turn is inside ``messages``, so it moved to the prompt column."""
        # Without the column the row looks empty — this is the regression the
        # fallback argument exists to prevent.
        assert system_opener(NEW_SHAPE_PAYLOAD, 200) is None
        assert (
            system_opener(NEW_SHAPE_PAYLOAD, 200, NEW_SHAPE_PROMPT)
            == "You are Claude Code, an agentic CLI."
        )

    def test_anthropic_system_field_needs_no_fallback(self):
        """``system`` is a top-level payload field and is never stripped."""
        payload = {"system": "Kilo Code agent", "max_tokens": 4096}
        assert system_opener(payload, 200) == "Kilo Code agent"
        assert system_opener(payload, 200, "[]") == "Kilo Code agent"

    def test_anthropic_system_blocks(self):
        payload = {"system": [{"type": "text", "text": "Kilo Code agent"}]}
        assert system_opener(payload, 200, NEW_SHAPE_PROMPT) == "Kilo Code agent"

    def test_system_message_content_blocks_in_the_prompt_column(self):
        prompt = json.dumps(
            [{"role": "system", "content": [{"type": "text", "text": "opencode here"}]}]
        )
        assert system_opener({}, 200, prompt) == "opencode here"

    def test_truncates_to_max_chars(self):
        prompt = json.dumps([{"role": "system", "content": "x" * 500}])
        assert system_opener({}, 10, prompt) == "x" * 10

    def test_anthropic_system_block_with_non_string_text(self):
        """A block whose ``text`` is not a string must not blow up the join."""
        payload = {"system": [{"type": "text", "text": 123}, {"type": "text", "text": "ok"}]}
        assert system_opener(payload, 200) == "123\nok"

    def test_openai_system_block_with_non_string_text(self):
        """Same coercion on the ``role: system`` message branch."""
        prompt = json.dumps(
            [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": 3.5},
                        {"type": "text", "text": "opencode here"},
                    ],
                }
            ]
        )
        assert system_opener({}, 200, prompt) == "3.5\nopencode here"


class TestUserMessages:
    def test_old_shape_row(self):
        assert user_messages(OLD_SHAPE_PAYLOAD, 200) == [
            "Fix the flaky test in test_router.py",
            "Also update the changelog",
        ]

    def test_new_shape_row(self):
        # No turns in the payload at all: without the column this is empty.
        assert user_messages(NEW_SHAPE_PAYLOAD, 200) == []
        assert user_messages(NEW_SHAPE_PAYLOAD, 200, NEW_SHAPE_PROMPT) == [
            "Fix the flaky test in test_router.py",
            "Also update the changelog",
        ]

    def test_prompt_column_wins_over_a_payload_copy(self):
        """A row carrying both must report the column's turns, not the copy."""
        both = {"messages": [{"role": "user", "content": "stale copy"}]}
        assert user_messages(both, 200, NEW_SHAPE_PROMPT) == [
            "Fix the flaky test in test_router.py",
            "Also update the changelog",
        ]

    def test_content_blocks_from_the_prompt_column(self):
        prompt = json.dumps(
            [{"role": "user", "content": [{"type": "text", "text": "refactor the router"}]}]
        )
        assert user_messages({}, 200, prompt) == ["refactor the router"]

    def test_decoded_list_column_works_too(self):
        """asyncpg hands back TEXT, but a caller may pass an already-decoded list."""
        assert user_messages({}, 200, _TURNS) == [
            "Fix the flaky test in test_router.py",
            "Also update the changelog",
        ]

    def test_truncates_each_turn(self):
        prompt = json.dumps([{"role": "user", "content": "y" * 500}])
        assert user_messages({}, 7, prompt) == ["y" * 7]

    def test_content_block_with_non_string_text(self):
        """A non-string ``text`` is rendered, not raised over.

        Live rows carry blocks like ``{"type": "text", "text": 123}``; before the
        value was coerced the join raised ``TypeError: sequence item 0: expected
        str instance, int found``, which 500s the admin Usage Insights endpoint
        that shares this helper.
        """
        prompt = json.dumps(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": 123},
                        {"type": "text", "text": "after the number"},
                    ],
                }
            ]
        )
        assert user_messages({}, 200, prompt) == ["123\nafter the number"]

    def test_null_block_text_drops_out_and_structured_text_becomes_json(self):
        prompt = json.dumps(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": None},
                        {"type": "text", "text": {"nested": 1}},
                        {"type": "text", "text": [1, 2]},
                        {"type": "text", "text": True},
                    ],
                }
            ]
        )
        assert user_messages({}, 200, prompt) == ['{"nested": 1}\n[1, 2]\ntrue']


class TestPayloadAsStoredText:
    def test_json_string_payload_and_prompt_are_both_decoded(self):
        """asyncpg returns JSONB/TEXT as str; the pair must survive that round trip."""
        payload = as_payload_dict(json.dumps(NEW_SHAPE_PAYLOAD))
        assert user_messages(payload, 200, NEW_SHAPE_PROMPT) == [
            "Fix the flaky test in test_router.py",
            "Also update the changelog",
        ]
        assert system_opener(payload, 200, NEW_SHAPE_PROMPT) == (
            "You are Claude Code, an agentic CLI."
        )
