"""Tests for output-token logprobs on OpenAI-compatible routes.

vLLM and sglang return ``logprobs`` when asked; most hosted APIs reject the
field or ignore it. So the request fields travel only to models whose
``supported_params`` declare them, and whatever the upstream returns has to
reach the client on every path: non-streaming, streamed, and streamed upstream
but buffered into one JSON body (``force_chat_completions_streaming``).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.schemas import ChatCompletionRequest, ChatCompletionResponse
from serving.servers.routers.completions import _streaming_response_with_keepalive

_TOKEN = {
    "token": "Yes",
    "logprob": -0.01,
    "bytes": [89, 101, 115],
    "top_logprobs": [
        {"token": "Yes", "logprob": -0.01, "bytes": [89, 101, 115]},
        {"token": "No", "logprob": -4.6, "bytes": [78, 111]},
    ],
}
_LOGPROBS = {"content": [_TOKEN]}

_RESPONSE = {
    "choices": [
        {
            "message": {"role": "assistant", "content": "Yes"},
            "finish_reason": "length",
            "logprobs": _LOGPROBS,
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
}


def _adapter(*supported: str) -> OpenAICompatAdapter:
    config = ModelConfig(
        id="allam-7b",
        name="ALLaM 7B",
        provider="vllm",
        base_url="http://mock.local/v1",
        provider_model_id="humain-ai/ALLaM-7B-Instruct-preview",
        supported_params=["temperature", "max_tokens", *supported],
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    return adapter


def _chunk(content: str, **choice: Any) -> str:
    return "data: " + json.dumps(
        {"id": "c1", "choices": [{"index": 0, "delta": {"content": content}, **choice}]}
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_logprobs_reach_a_model_that_declares_them():
    adapter = _adapter("logprobs", "top_logprobs")
    post = AsyncMock(return_value=_RESPONSE)
    adapter._post_with_pool = post

    await adapter.chat_completion(
        [{"role": "user", "content": "hi"}], logprobs=True, top_logprobs=5, max_tokens=1
    )

    sent = post.call_args.args[1]
    assert sent["logprobs"] is True
    assert sent["top_logprobs"] == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_logprobs_never_reach_a_model_that_does_not_declare_them():
    adapter = _adapter()
    post = AsyncMock(return_value=_RESPONSE)
    adapter._post_with_pool = post

    await adapter.chat_completion(
        [{"role": "user", "content": "hi"}], logprobs=True, top_logprobs=5
    )

    sent = post.call_args.args[1]
    assert "logprobs" not in sent
    assert "top_logprobs" not in sent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_non_streaming_answer_keeps_the_upstream_logprobs():
    adapter = _adapter("logprobs", "top_logprobs")
    adapter._post_with_pool = AsyncMock(return_value=_RESPONSE)

    result = await adapter.chat_completion(
        [{"role": "user", "content": "hi"}], logprobs=True, top_logprobs=2
    )

    assert result["choices"][0]["logprobs"] == _LOGPROBS
    # The route's response model, which the batch path applies too, keeps them.
    body = ChatCompletionResponse.model_validate(result).model_dump(exclude_none=True)
    assert body["choices"][0]["logprobs"] == _LOGPROBS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_streamed_answer_keeps_the_upstream_logprobs():
    adapter = _adapter("logprobs", "top_logprobs")

    async def _stream_post(*, url, json, headers, timeout):
        yield _chunk("Yes", logprobs=_LOGPROBS, finish_reason="length")
        yield "data: [DONE]"

    adapter.http.stream_post = _stream_post
    frames = [
        frame
        async for frame in adapter.stream_chat_completion(
            [{"role": "user", "content": "hi"}], stream=True, logprobs=True
        )
    ]

    choices = [
        choice
        for frame in frames
        if frame.startswith("data: {")
        for choice in json.loads(frame[6:]).get("choices") or []
    ]
    assert any(choice.get("logprobs") == _LOGPROBS for choice in choices)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_buffered_stream_joins_the_logprobs_of_every_chunk():
    second = {**_TOKEN, "token": "!"}

    async def _chunks():
        yield _chunk("Yes", logprobs={"content": [_TOKEN]})
        yield _chunk("!", logprobs={"content": [second]}, finish_reason="stop")
        yield "data: [DONE]"

    body = b"".join(
        [
            part
            async for part in _streaming_response_with_keepalive(
                _chunks(), request_id="req_1", model="allam-7b", request_headers={}
            )
        ]
    )

    choice = json.loads(body)["choices"][0]
    assert choice["message"]["content"] == "Yes!"
    assert choice["logprobs"] == {"content": [_TOKEN, second]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_buffered_stream_without_logprobs_adds_none():
    async def _chunks():
        yield _chunk("Yes", finish_reason="stop")
        yield "data: [DONE]"

    body = b"".join(
        [
            part
            async for part in _streaming_response_with_keepalive(
                _chunks(), request_id="req_1", model="allam-7b", request_headers={}
            )
        ]
    )

    assert "logprobs" not in json.loads(body)["choices"][0]


@pytest.mark.unit
def test_top_logprobs_is_capped_at_twenty():
    message = [{"role": "user", "content": "hi"}]
    assert ChatCompletionRequest(model="m", messages=message, top_logprobs=20).top_logprobs == 20
    with pytest.raises(ValidationError):
        ChatCompletionRequest(model="m", messages=message, top_logprobs=21)
