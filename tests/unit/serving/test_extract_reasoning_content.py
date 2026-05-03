from __future__ import annotations

import json

import pytest

from serving.servers.routers.admin.metrics import extract_reasoning_content


@pytest.mark.unit
def test_extract_reasoning_content_none_returns_none():
    assert extract_reasoning_content(None) is None
    assert extract_reasoning_content("") is None


@pytest.mark.unit
def test_extract_reasoning_content_malformed_json_returns_none():
    assert extract_reasoning_content("not-json") is None
    assert extract_reasoning_content("{not: valid}") is None


@pytest.mark.unit
def test_extract_reasoning_content_openai_choices_reasoning_content():
    payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "hello",
                        "reasoning_content": "thought one",
                    }
                },
                {
                    "message": {
                        "role": "assistant",
                        "content": "world",
                        "reasoning_content": "thought two",
                    }
                },
            ]
        }
    )
    assert extract_reasoning_content(payload) == "thought one\n\nthought two"


@pytest.mark.unit
def test_extract_reasoning_content_openai_choices_reasoning_fallback():
    payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "hello",
                        "reasoning": "fallback thought",
                    }
                }
            ]
        }
    )
    assert extract_reasoning_content(payload) == "fallback thought"


@pytest.mark.unit
def test_extract_reasoning_content_anthropic_thinking_blocks():
    payload = json.dumps(
        {
            "content": [
                {"type": "thinking", "thinking": "anthropic step 1"},
                {"type": "text", "text": "answer"},
                {"type": "thinking", "thinking": "anthropic step 2"},
            ]
        }
    )
    assert extract_reasoning_content(payload) == "anthropic step 1\n\nanthropic step 2"


@pytest.mark.unit
def test_extract_reasoning_content_messages_field():
    payload = json.dumps(
        {
            "messages": [
                {"role": "assistant", "reasoning_content": "msg-level thought"},
            ]
        }
    )
    assert extract_reasoning_content(payload) == "msg-level thought"


@pytest.mark.unit
def test_extract_reasoning_content_strips_whitespace():
    payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "  \n\nthought one\n\n  ",
                    }
                },
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "\n\nthought two  ",
                    }
                },
            ]
        }
    )
    assert extract_reasoning_content(payload) == "thought one\n\nthought two"


@pytest.mark.unit
def test_extract_reasoning_content_whitespace_only_treated_as_empty():
    payload = json.dumps(
        {"choices": [{"message": {"role": "assistant", "reasoning_content": "   \n\n  "}}]}
    )
    assert extract_reasoning_content(payload) is None


@pytest.mark.unit
def test_extract_reasoning_content_empty_returns_none():
    assert extract_reasoning_content(json.dumps({"choices": []})) is None
    assert (
        extract_reasoning_content(
            json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "no reasoning here"}}]}
            )
        )
        is None
    )
    assert extract_reasoning_content(json.dumps({})) is None
    assert extract_reasoning_content(json.dumps([1, 2, 3])) is None
