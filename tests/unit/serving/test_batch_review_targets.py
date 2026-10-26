"""Independent no-silent-loss regressions for Batch target admission."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from serving.batch import targets


async def test_responses_rejects_input_file_instead_of_dropping_attachment(monkeypatch):
    monkeypatch.setattr(targets, "_cached_route_model", AsyncMock(return_value=("model", None)))
    body = {
        "model": "model",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Summarize attachment"},
                    {"type": "input_file", "file_id": "file-attachment"},
                ],
            }
        ],
    }
    with pytest.raises(targets.ItemRejected):
        await targets._admit_responses(body, None)


@pytest.mark.parametrize(
    "metadata",
    [
        {"cache_control": {"type": "ephemeral"}},
        {"citations": [{"type": "char_location", "cited_text": "result"}]},
    ],
)
def test_translated_tool_result_rejects_native_nested_text_metadata(metadata):
    body = {
        "model": "model",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool1",
                        "content": [{"type": "text", "text": "result", **metadata}],
                    }
                ],
            }
        ],
    }
    assert targets.openai_translation_problem(body) is not None


def test_messages_tool_requirement_rejects_adapter_without_tools():
    body = {
        "model": "model",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "use tool"}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
    }
    adapter = SimpleNamespace(
        native_format="openai",
        config=SimpleNamespace(
            supports_tools=False,
            supported_params=["max_tokens"],
        ),
    )
    assert targets.adapter_problem(adapter, body) is not None


@pytest.mark.parametrize(
    "text",
    [
        {"verbosity": "low"},
        {"format": {"type": "unsupported-format"}},
    ],
)
async def test_responses_rejects_text_options_without_translation(monkeypatch, text):
    from serving.responses_translator import responses_request_to_chat_params

    monkeypatch.setattr(targets, "_cached_route_model", AsyncMock(return_value=("model", None)))
    body = {"model": "model", "input": "hello", "text": text}
    params, _dropped = responses_request_to_chat_params(body)
    assert "response_format" not in params
    assert "verbosity" not in params
    with pytest.raises(targets.ItemRejected):
        await targets._admit_responses(body, None)


@pytest.mark.parametrize(
    "output",
    [
        [{"type": "input_image", "image_url": "https://example.test/result.png"}],
        [{"type": "input_file", "file_id": "file-result"}],
    ],
)
async def test_responses_rejects_multimodal_tool_output_instead_of_stringifying(
    monkeypatch, output
):
    from serving.responses_translator import responses_input_to_messages

    monkeypatch.setattr(targets, "_cached_route_model", AsyncMock(return_value=("model", None)))
    body = {
        "model": "model",
        "input": [
            {"type": "function_call_output", "call_id": "call-review", "output": output},
        ],
    }
    translated = responses_input_to_messages(body["input"])
    assert isinstance(translated[0]["content"], str)
    assert output[0]["type"] in translated[0]["content"]
    with pytest.raises(targets.ItemRejected):
        await targets._admit_responses(body, None)
