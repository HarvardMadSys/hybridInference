"""The usage estimate an adapter makes when the upstream reports none is bounded.

At the end of every stream whose upstream sent no usage -- and on Gemini's
non-streaming path too -- the adapters tokenized the whole prompt on the event
loop: about 211 ms per 700 KB, and the prompt is the caller's size. They now use
the bounded estimator, which counts a prompt within the inline bound exactly
and samples a longer one.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.gemini import GeminiAdapter
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.utils import tokens
from serving.utils.tokens import INLINE_ESTIMATE_MAX_CHARS, estimate_prompt_tokens

pytestmark = pytest.mark.unit

SMALL = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello there"}]
LARGE = [{"role": "user", "content": "lorem ipsum dolor sit amet " * 80_000}]  # ~2 MB


@pytest.fixture
def tokenized(monkeypatch) -> list[int]:
    """The length of every text the tokenizer is asked to count."""
    seen: list[int] = []
    real = tokens._count_with_tiktoken

    def spy(text: str) -> int:
        seen.append(len(text))
        return real(text)

    monkeypatch.setattr(tokens, "_count_with_tiktoken", spy)
    return seen


class _StreamHTTP:
    """An upstream stream that reports no usage."""

    async def stream_post(self, url: str, **_kw: Any):
        chunk = {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(chunk)}"
        yield "data: [DONE]"


async def _openai_stream_usage(messages: list[dict[str, Any]]) -> dict[str, Any]:
    adapter = OpenAICompatAdapter(
        ModelConfig(id="m", name="m", provider="p", base_url="https://api.example/v1", api_key="k")
    )
    adapter.http = _StreamHTTP()
    frames = [frame async for frame in adapter.stream_chat_completion(messages)]
    return json.loads(frames[-2].removeprefix("data: "))["usage"]


@pytest.mark.asyncio
async def test_a_short_prompt_is_still_counted_exactly():
    usage = await _openai_stream_usage(SMALL)

    assert usage["prompt_tokens"] == estimate_prompt_tokens(SMALL)


@pytest.mark.asyncio
async def test_a_long_prompt_is_not_tokenized_whole_at_stream_end(tokenized):
    usage = await _openai_stream_usage(LARGE)

    assert usage["prompt_tokens"] > 0
    # The sample, and the two-character answer: not the 2 MB prompt.
    assert sum(tokenized) <= INLINE_ESTIMATE_MAX_CHARS + 4096


def _gemini(reply: dict[str, Any]) -> GeminiAdapter:
    adapter = GeminiAdapter(
        ModelConfig(id="g", name="g", provider="gemini", base_url="https://mock", api_key="k")
    )
    adapter.http = MagicMock()
    adapter.http.json_post_with_retry = AsyncMock(return_value=reply)
    return adapter


_NO_USAGE = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}


@pytest.mark.asyncio
async def test_gemini_counts_a_short_prompt_exactly_without_usage_metadata():
    response = await _gemini(_NO_USAGE).chat_completion(SMALL)

    assert response["usage"]["prompt_tokens"] == estimate_prompt_tokens(SMALL)


@pytest.mark.asyncio
async def test_gemini_does_not_tokenize_a_long_prompt_whole(tokenized):
    response = await _gemini(_NO_USAGE).chat_completion(LARGE)

    assert response["usage"]["prompt_tokens"] > 0
    assert sum(tokenized) <= INLINE_ESTIMATE_MAX_CHARS + 4096


@pytest.mark.asyncio
async def test_gemini_does_not_tokenize_a_long_prompt_whole_at_stream_end(tokenized):
    adapter = _gemini({})

    async def stream_post(url: str, **_kw: Any):
        yield f"data: {json.dumps(_NO_USAGE)}"

    adapter.http.stream_post = stream_post
    frames = [frame async for frame in adapter.stream_chat_completion(LARGE)]

    usage = json.loads(frames[-2].removeprefix("data: "))["usage"]
    assert usage["prompt_tokens"] > 0
    assert sum(tokenized) <= INLINE_ESTIMATE_MAX_CHARS + 4096
