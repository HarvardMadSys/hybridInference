"""Regression tests for Gemini adapter streaming robustness and tool-call history.

Covers two bugs:
- A1 (#874): streaming must not crash on frames without ``candidates`` (blocked
  prompt, usage-only, in-band error) or with an empty ``candidates`` list.
- A2 (#875): OpenAI->Gemini history translation must emit assistant tool_calls as
  functionCall parts and name the role:"tool" functionResponse after the matching
  prior call, not the literal "tool".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.gemini import GeminiAdapter


def _make_adapter() -> GeminiAdapter:
    config = ModelConfig(
        id="gemini-test",
        name="Gemini Test",
        provider="gemini",
        base_url="https://mock",
        api_key="test-key",
    )
    adapter = GeminiAdapter(config)
    adapter.http = MagicMock()
    return adapter


@pytest.mark.asyncio
async def test_stream_survives_frames_without_candidates():
    """A1: a promptFeedback frame, an empty-candidates frame, and a usage-only
    frame must not raise and must not emit bogus content."""
    lines = [
        # Blocked-prompt frame: no "candidates" key at all.
        json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}),
        # Empty candidates list: must not IndexError.
        json.dumps({"candidates": []}),
        # A well-formed text frame in the middle to prove normal flow still works.
        json.dumps({"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]}),
        # Usage-only frame: no candidates, carries finishReason nowhere.
        json.dumps(
            {
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 4,
                }
            }
        ),
    ]

    async def _fake_stream(*_args, **_kwargs):
        for line in lines:
            yield line

    adapter = _make_adapter()
    adapter.http.stream_post = MagicMock(return_value=_fake_stream())

    text_pieces: list[str] = []
    # Must complete without raising NameError/IndexError.
    async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
        raw = chunk[6:] if chunk.startswith("data: ") else chunk
        raw = raw.strip()
        if not raw or raw == "[DONE]":
            continue
        obj = json.loads(raw)
        for choice in obj.get("choices", []):
            content = choice.get("delta", {}).get("content")
            if content:
                text_pieces.append(content)

    # Only the single well-formed "Hi" should have been emitted; the candidate-less
    # frames must not have produced spurious empty finishReason chunks or content.
    assert "".join(text_pieces) == "Hi"


def test_convert_messages_translates_tool_calls_and_response_name():
    """A2: assistant tool_calls become a functionCall turn, and the tool result's
    functionResponse is named after the matching call (not the literal "tool")."""
    adapter = _make_adapter()
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]

    body = adapter._convert_messages_to_gemini(messages)
    contents = body["contents"]

    # Locate the functionCall part and the functionResponse part.
    call_idx = None
    response_idx = None
    for idx, turn in enumerate(contents):
        for part in turn.get("parts", []):
            if "functionCall" in part:
                assert part["functionCall"]["name"] == "get_weather"
                assert part["functionCall"]["args"] == {"city": "NYC"}
                call_idx = idx
            if "functionResponse" in part:
                assert part["functionResponse"]["name"] == "get_weather"
                response_idx = idx

    assert call_idx is not None, "assistant tool_calls turn was dropped"
    assert response_idx is not None, "tool functionResponse turn missing"
    # The functionCall turn must precede the functionResponse turn.
    assert call_idx < response_idx
