"""Tests for a route whose address is answered by an admission process.

A Nimbus router in admission-only mode sits beside a self-hosted engine and holds
its queue. A request it judges the engine cannot serve in time comes back with a
409 whose body has a ``nimbus`` object: the request reached no engine. The
adapter raises that as ``EngineDeclined`` for a route that declared the process
(``ModelConfig.admission``) and for no other, so a 409 from any other upstream
keeps meaning what that upstream meant.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from routing.endpoint_health import http_status_of
from routing.telemetry import is_uncharged_failure
from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.adapters.upstream_limiter import (
    EngineDeclined,
    UpstreamQueueWaitExpired,
    UpstreamSaturated,
)

_DECLINE = {
    "error": {"message": "send it remote", "type": "nimbus_declined", "code": "declined_at_cutoff"},
    "nimbus": {"request_id": "r17", "decision": "declined_at_cutoff", "ttft_left_ms": 812.5},
}
_ASK = [{"role": "user", "content": "hi"}]


def _adapter(*, admission: str | None, **overrides: Any) -> OpenAICompatAdapter:
    config = ModelConfig(
        id="qwen-local",
        name="Qwen local",
        provider="sglang",
        base_url="http://mock.local/v1",
        provider_model_id="qwen-local",
        supported_params=["temperature", "max_tokens"],
        admission=admission,
        **overrides,
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    return adapter


def _answer(status: int, body: Any) -> aiohttp.ClientResponseError:
    """The error the HTTP client raises for an upstream answer of ``status``."""
    error = aiohttp.ClientResponseError(
        request_info=MagicMock(), history=(), status=status, message="upstream said no"
    )
    error.error_body = body if isinstance(body, str) else json.dumps(body)  # type: ignore[attr-defined]
    return error


async def _chat(adapter: OpenAICompatAdapter, error: BaseException) -> None:
    adapter.http.json_post_with_retry = AsyncMock(side_effect=error)
    await adapter.chat_completion(_ASK)


async def _stream(adapter: OpenAICompatAdapter, error: BaseException) -> None:
    async def _stream_post(*, url, json, headers, timeout):
        raise error
        yield ""  # pragma: no cover - makes this an async generator

    adapter.http.stream_post = _stream_post
    async for _ in adapter.stream_chat_completion(_ASK):
        pass


@pytest.mark.parametrize("send", [_chat, _stream], ids=["chat", "stream"])
async def test_a_declared_routes_decline_is_raised_as_engine_declined(send):
    with pytest.raises(EngineDeclined) as caught:
        await send(_adapter(admission="nimbus"), _answer(409, _DECLINE))

    declined = caught.value
    assert declined.decision == "declined_at_cutoff"
    assert declined.ttft_left_ms == 812.5
    assert declined.engine_request_id == "r17"
    # The answer it came from stays attached for whoever debugs it.
    assert isinstance(declined.__cause__, aiohttp.ClientResponseError)


@pytest.mark.parametrize("send", [_chat, _stream], ids=["chat", "stream"])
async def test_a_route_that_declares_nothing_keeps_the_409(send):
    # Another gateway or provider may answer 409 for reasons of its own; even a
    # body shaped like a decline is not one unless the route said who answers.
    with pytest.raises(aiohttp.ClientResponseError) as caught:
        await send(_adapter(admission=None), _answer(409, _DECLINE))

    assert not isinstance(caught.value, EngineDeclined)
    assert caught.value.status == 409


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (409, {"error": {"message": "conflict"}}),
        (409, {"nimbus": "declined"}),
        (409, "not json"),
        (409, ""),
        (400, _DECLINE),
        (503, _DECLINE),
    ],
)
@pytest.mark.parametrize("send", [_chat, _stream], ids=["chat", "stream"])
async def test_only_a_409_with_the_processes_object_is_a_decline(send, status, body):
    with pytest.raises(aiohttp.ClientResponseError) as caught:
        await send(_adapter(admission="nimbus"), _answer(status, body))

    assert caught.value.status == status


async def test_a_decline_that_reports_little_is_still_a_decline():
    with pytest.raises(EngineDeclined) as caught:
        await _chat(_adapter(admission="nimbus"), _answer(409, {"nimbus": {"ttft_left_ms": True}}))

    declined = caught.value
    assert declined.decision == "declined"
    assert declined.ttft_left_ms is None
    assert declined.engine_request_id is None


@pytest.mark.parametrize("mode", ["chat", "stream"])
async def test_a_decline_is_not_retried_on_another_key_and_mutes_none(mode):
    # Every key of the route reaches the same process, which already answered:
    # a second key would only ask it again, later, with less of the budget left.
    adapter = _adapter(admission="nimbus", api_keys=["sk-test-first-key", "sk-test-second-key"])
    sent: list[str] = []

    async def json_post(*, url, json, headers, timeout, **_limit):
        sent.append(headers["Authorization"])
        raise _answer(409, _DECLINE)

    async def stream_post(*, url, json, headers, timeout):
        sent.append(headers["Authorization"])
        raise _answer(409, _DECLINE)
        yield ""  # pragma: no cover - makes this an async generator

    adapter.http.json_post = json_post
    adapter.http.stream_post = stream_post

    with pytest.raises(EngineDeclined):
        if mode == "chat":
            await adapter.chat_completion(_ASK)
        else:
            async for _ in adapter.stream_chat_completion(_ASK):
                pass

    assert len(sent) == 1  # one key asked, once
    for key in adapter._key_pool._keys:
        assert key.cooldown_until == 0.0
        assert key.consecutive_failures == 0


def test_a_decline_reached_no_engine_and_blames_no_endpoint():
    declined = EngineDeclined("declined_at_arrival", ttft_left_ms=10.0)

    # Read like a wait that ended in this gateway's own engine queue.
    assert isinstance(declined, UpstreamSaturated)
    assert not isinstance(declined, UpstreamQueueWaitExpired)
    assert is_uncharged_failure(declined)
    # No status to mistake for the upstream's verdict on the request.
    assert http_status_of(declined) is None
    for name in ("status", "status_code", "code"):
        assert not hasattr(declined, name)


def test_admission_is_off_unless_declared_and_takes_one_value():
    config = ModelConfig(id="m", name="m", provider="sglang", base_url="http://mock.local/v1")
    assert config.admission is None

    with pytest.raises(ValueError, match="admission"):
        ModelConfig(
            id="m", name="m", provider="sglang", base_url="http://mock.local/v1", admission="yes"
        )
