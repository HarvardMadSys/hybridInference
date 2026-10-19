"""What /v1/chat/completions promises a caller, checked through the real route.

The gateway runs in process (httpx ``ASGITransport``) with its real auth
dependency, router and request handling; only the upstream is a recording fake.
Each test is about the gateway's own behaviour, so no provider is contacted:

- ``X-Route-Pin`` forces a backend for admins only; anyone else's is ignored.
- Every sampling and tool parameter a caller sets reaches the adapter.
- A malformed ``usage`` block from an upstream does not fail a request that
  succeeded: an unreadable count is billed as 0 and logged.
- When the gateway buffers an upstream stream for a non-streaming caller, an
  upstream failure after the first keepalive byte still ends in an error body.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openai.types import CompletionUsage

from routing.executor import RouteExecutor
from routing.routers import ProviderPinError
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import completions
from serving.servers.routers.completions_cost import CostTracker
from serving.storage.utils import billable_output_tokens
from serving.stream import done_sentinel

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.asyncio

_MODEL = "contract-model"
_HI = [{"role": "user", "content": "Hi"}]


class RecordingAdapter(BaseAdapter):
    """A fake upstream that records what it was asked and answers as told."""

    def __init__(
        self,
        provider: str,
        *,
        usage: dict[str, Any] | None = None,
        stream_script: list[Any] | None = None,
    ) -> None:
        super().__init__(
            ModelConfig(
                id=_MODEL,
                name=_MODEL,
                provider=provider,
                base_url=f"http://{provider}.test",
                context_length=8192,
                max_output_length=4096,
            )
        )
        self.calls: list[dict[str, Any]] = []
        self._usage = usage
        # Items yielded in order; a float sleeps, an exception is raised.
        self._stream_script = stream_script

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        self.calls.append(params)
        response = self.format_response(content=f"from {self.config.provider}", model=_MODEL)
        if self._usage is not None:
            response["usage"] = self._usage
        return response

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self.calls.append(params)
        script = self._stream_script or [
            self.format_stream_chunk(model=_MODEL, content=f"from {self.config.provider}"),
            done_sentinel(),
        ]
        for item in script:
            if isinstance(item, float):
                await asyncio.sleep(item)
            elif isinstance(item, BaseException):
                raise item
            else:
                yield item


def _user(*, admin: bool) -> dict[str, Any]:
    return {
        "user_id": "u-admin" if admin else "u-member",
        "user_name": "admin" if admin else "member",
        "role": "admin" if admin else "pro",
        "authenticated": True,
        "quota_remaining_cost_usd": 100.0,
        "is_admin": admin,
    }


def _app(
    monkeypatch,
    adapters: list[RecordingAdapter],
    *,
    user: dict[str, Any] | None = None,
    force_streaming: bool = False,
) -> FastAPI:
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    router = RouteExecutor()
    router.register_route(_MODEL, [(adapter, 1.0) for adapter in adapters])
    runtime_settings = MagicMock()
    runtime_settings.get_bool = AsyncMock(return_value=force_streaming)
    app = FastAPI()
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router, db_logger=None, log_store=None, runtime_settings=runtime_settings
    )
    install_error_handlers(app)
    app.include_router(completions.router)
    if user is not None:
        app.dependency_overrides[verify_api_key] = lambda: user
    return app


async def _post(app: FastAPI, *, headers: dict[str, str] | None = None, **body: Any):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/chat/completions",
            json={"model": _MODEL, "messages": _HI, **body},
            headers=headers or {},
        )


# --- X-Route-Pin -------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
async def test_an_admin_pin_forces_the_backend(monkeypatch, stream):
    alpha, beta = RecordingAdapter("alpha"), RecordingAdapter("beta")
    app = _app(monkeypatch, [alpha, beta], user=_user(admin=True))

    for _ in range(6):
        response = await _post(app, headers={"X-Route-Pin": "beta"}, stream=stream)
        assert response.status_code == 200, response.text

    assert (len(alpha.calls), len(beta.calls)) == (0, 6)


@pytest.mark.parametrize("stream", [False, True])
async def test_an_admin_pin_to_an_unknown_backend_is_a_clean_400(monkeypatch, stream):
    alpha = RecordingAdapter("alpha")
    app = _app(monkeypatch, [alpha], user=_user(admin=True))

    response = await _post(app, headers={"X-Route-Pin": "ghost"}, stream=stream)

    assert response.status_code == 400
    assert "ghost" in response.text
    assert alpha.calls == []


@pytest.mark.parametrize("stream", [False, True])
async def test_a_members_pin_is_ignored(monkeypatch, stream):
    """Not refused, ignored: a non-admin's pin neither forces nor fails routing.

    A pin naming no backend at all is the observable proof: honoured, it would
    be the admin's 400 above.
    """
    alpha = RecordingAdapter("alpha")
    app = _app(monkeypatch, [alpha], user=_user(admin=False))

    response = await _post(app, headers={"X-Route-Pin": "ghost"}, stream=stream)

    assert response.status_code == 200, response.text
    assert len(alpha.calls) == 1


async def test_with_auth_disabled_the_caller_is_an_admin_and_may_pin(monkeypatch):
    """By design (``verify_api_key``): no auth means an anonymous admin.

    Recorded here so that changing it is a decision, not a side effect.
    """
    alpha, beta = RecordingAdapter("alpha"), RecordingAdapter("beta")
    app = _app(monkeypatch, [alpha, beta])  # the real dependency, auth off

    for _ in range(4):
        assert (await _post(app, headers={"X-Route-Pin": "beta"})).status_code == 200
    assert (await _post(app, headers={"X-Route-Pin": "ghost"})).status_code == 400

    assert (len(alpha.calls), len(beta.calls)) == (0, 4)


async def test_a_pin_the_router_cannot_honour_at_dispatch_is_a_400(monkeypatch):
    """The backend exists but cannot be dispatched to (e.g. its circuit is open)."""
    alpha = RecordingAdapter("alpha")
    app = _app(monkeypatch, [alpha], user=_user(admin=True))
    router = app.state.services.router

    async def _refuse(*_args, **_kwargs):
        raise ProviderPinError("Pinned provider 'alpha' not found for model contract-model")

    monkeypatch.setattr(router, "chat_completion", _refuse)

    response = await _post(app, headers={"X-Route-Pin": "alpha"})

    assert response.status_code == 400


# --- parameters reach the adapter --------------------------------------------

_PARAMS: dict[str, Any] = {
    "temperature": 0.3,
    "top_p": 0.9,
    "top_k": 40,
    "min_p": 0.05,
    "max_tokens": 64,
    "stop": ["END", "STOP"],
    "seed": 1234,
    "frequency_penalty": 0.5,
    "presence_penalty": -0.25,
    "reasoning_effort": "high",
    "thinking": {"type": "enabled", "budget_tokens": 512},
    "tools": [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}},
        }
    ],
    "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    "response_format": {"type": "json_object"},
}


@pytest.mark.parametrize("stream", [False, True])
async def test_every_parameter_a_caller_sets_reaches_the_adapter(monkeypatch, stream):
    alpha = RecordingAdapter("alpha")
    app = _app(monkeypatch, [alpha])

    response = await _post(app, stream=stream, **_PARAMS)

    assert response.status_code == 200, response.text
    [received] = alpha.calls
    assert {name: received.get(name) for name in _PARAMS} == _PARAMS
    assert received["stream"] is stream


async def test_a_parameter_left_unset_is_not_sent_as_null(monkeypatch):
    """Absent stays absent: a provider may reject an explicit null."""
    alpha = RecordingAdapter("alpha")
    app = _app(monkeypatch, [alpha])

    response = await _post(app, temperature=0.3)

    assert response.status_code == 200, response.text
    [received] = alpha.calls
    assert received["temperature"] == 0.3
    assert not set(_PARAMS) - {"temperature"} & set(received)


# --- a malformed usage block -------------------------------------------------


# An upstream that answered has been paid for. A count that cannot be read is
# billed as 0 and logged (serving.utils.token_utils.usage_count); the request
# succeeds, and the body reports what was billed.
_MALFORMED_USAGE = [
    pytest.param(
        {"prompt_tokens": "lots", "completion_tokens": 3, "total_tokens": 4},
        {"prompt_tokens": 0, "completion_tokens": 3, "total_tokens": 4},
        id="text",
    ),
    pytest.param(
        {"prompt_tokens": 5, "completion_tokens": None, "total_tokens": "?"},
        {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
        id="null-and-text",
    ),
    pytest.param(
        {"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1},
        {"prompt_tokens": 0, "completion_tokens": 2, "total_tokens": 1},
        id="negative",
    ),
    pytest.param(
        {"prompt_tokens": "12", "completion_tokens": 3.0, "total_tokens": "15"},
        {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        id="numeric-strings",
    ),
    pytest.param(
        {"prompt_tokens": 5, "completion_tokens": 3, "prompt_tokens_details": "n/a"},
        {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        id="odd-details-and-no-total",
    ),
]


@pytest.mark.parametrize(("usage", "billed"), _MALFORMED_USAGE)
async def test_a_malformed_usage_block_is_billed_as_read_not_failed(monkeypatch, usage, billed):
    alpha = RecordingAdapter("alpha", usage=usage)
    app = _app(monkeypatch, [alpha])
    increments: list[dict[str, Any]] = []
    original = CostTracker.schedule_increment

    async def _record(self, **kwargs):
        increments.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(CostTracker, "schedule_increment", _record)

    response = await _post(app)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "from alpha"
    assert {k: body["usage"][k] for k in billed} == billed
    assert "prompt_tokens_details" not in body["usage"] or isinstance(
        body["usage"]["prompt_tokens_details"], dict
    )
    [charged] = increments
    assert (charged["prompt_tokens"], charged["completion_tokens"]) == (
        billed["prompt_tokens"],
        billed["completion_tokens"],
    )


async def _charged(monkeypatch) -> list[dict[str, Any]]:
    increments: list[dict[str, Any]] = []
    original = CostTracker.schedule_increment

    async def _record(self, **kwargs):
        increments.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(CostTracker, "schedule_increment", _record)
    return increments


def _stream_with_usage(usage: dict[str, Any]) -> list[Any]:
    usage_chunk = {
        "id": "chatcmpl-usage",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": _MODEL,
        "choices": [],
        "usage": usage,
    }
    return [
        RecordingAdapter("alpha").format_stream_chunk(model=_MODEL, content="streamed"),
        f"data: {json.dumps(usage_chunk)}\n\n",
        done_sentinel(),
    ]


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 5, "completion_tokens": 3, "reasoning_tokens": 4},
        {"prompt_tokens": 5, "completion_tokens": 3, "reasoning_tokens": 4, "total_tokens": "?"},
    ],
    ids=["no-total", "unreadable-total"],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_reasoning_is_billed_the_same_without_a_reported_total(monkeypatch, usage, stream):
    """Without a reported total, reasoning counts on top of completion (3 + 4).

    A total the gateway fills in for the response model must never reach
    billing: equal to prompt + completion, it reads as "reasoning already
    included" and would charge 3.
    """
    script = _stream_with_usage(usage) if stream else None
    alpha = RecordingAdapter("alpha", usage=None if stream else usage, stream_script=script)
    app = _app(monkeypatch, [alpha])
    increments = await _charged(monkeypatch)

    response = await _post(app, stream=stream)

    assert response.status_code == 200, response.text
    [charged] = increments
    assert charged["total_tokens"] is None
    assert (
        billable_output_tokens(
            prompt_tokens=charged["prompt_tokens"],
            completion_tokens=charged["completion_tokens"],
            reasoning_tokens=charged["reasoning_tokens"],
            total_tokens=charged["total_tokens"],
        )
        == 7
    )
    if not stream:
        assert response.json()["usage"]["total_tokens"] == 8  # the client's copy, filled in


async def test_a_stream_tells_the_client_the_counts_that_were_billed(monkeypatch):
    alpha = RecordingAdapter(
        "alpha",
        stream_script=_stream_with_usage(
            {"prompt_tokens": "lots", "completion_tokens": "7", "total_tokens": 7}
        ),
    )
    app = _app(monkeypatch, [alpha])
    increments = await _charged(monkeypatch)

    response = await _post(app, stream=True)

    [usage_event] = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: {") and '"usage"' in line
    ]
    assert {k: usage_event["usage"][k] for k in ("prompt_tokens", "completion_tokens")} == {
        "prompt_tokens": 0,
        "completion_tokens": 7,
    }
    [charged] = increments
    assert (charged["prompt_tokens"], charged["completion_tokens"]) == (0, 7)


_CLIENT_USAGE_CASES = [
    pytest.param({"prompt_tokens": 5, "completion_tokens": 3}, id="no-total"),
    pytest.param({"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": "?"}, id="bad-total"),
    pytest.param({"prompt_tokens": 5, "completion_tokens": None}, id="null-completion"),
    pytest.param({"prompt_tokens": "lots", "completion_tokens": 3, "total_tokens": 4}, id="text"),
    pytest.param(
        {"prompt_tokens": 5, "completion_tokens": 3, "prompt_tokens_details": "n/a"},
        id="odd-details",
    ),
]


def _client_usages(response, path: str) -> list[dict[str, Any]]:
    if path == "stream":
        return [
            json.loads(line[6:])["usage"]
            for line in response.text.splitlines()
            if line.startswith("data: {") and '"usage"' in line
        ]
    return [json.loads(response.content)["usage"]]


@pytest.mark.parametrize("usage", _CLIENT_USAGE_CASES)
@pytest.mark.parametrize("path", ["non-stream", "stream", "buffered"])
async def test_every_path_hands_the_client_a_valid_openai_usage(monkeypatch, usage, path):
    """A strict client validates usage with the OpenAI SDK's own model.

    The internal billing copy keeps "not reported"; only what is sent is completed.
    """
    stream_script = None if path == "non-stream" else _stream_with_usage(usage)
    alpha = RecordingAdapter(
        "alpha", usage=usage if path == "non-stream" else None, stream_script=stream_script
    )
    app = _app(monkeypatch, [alpha], force_streaming=path == "buffered")

    response = await _post(app, stream=path == "stream")

    assert response.status_code == 200, response.text
    [sent] = _client_usages(response, path)
    CompletionUsage.model_validate(sent)
    assert isinstance(sent["total_tokens"], int)


async def test_an_unreadable_count_is_logged_for_follow_up(monkeypatch, caplog):
    alpha = RecordingAdapter("alpha", usage={"prompt_tokens": "lots", "completion_tokens": 3})
    app = _app(monkeypatch, [alpha])

    with caplog.at_level("WARNING", logger="serving.utils.token_utils"):
        assert (await _post(app)).status_code == 200

    assert "Unreadable upstream prompt_tokens 'lots'" in caplog.text


@pytest.mark.parametrize(("usage", "_billed"), _MALFORMED_USAGE)
async def test_a_malformed_usage_chunk_does_not_break_a_stream(monkeypatch, usage, _billed):
    usage_chunk = {
        "id": "chatcmpl-usage",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": _MODEL,
        "choices": [],
        "usage": usage,
    }
    alpha = RecordingAdapter(
        "alpha",
        stream_script=[
            RecordingAdapter("alpha").format_stream_chunk(model=_MODEL, content="streamed"),
            f"data: {json.dumps(usage_chunk)}\n\n",
            done_sentinel(),
        ],
    )
    app = _app(monkeypatch, [alpha])

    response = await _post(app, stream=True)

    assert response.status_code == 200, response.text
    assert '"streamed"' in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


# --- buffered streaming: failures after the first byte -----------------------


@pytest.fixture
def quick_keepalive(monkeypatch):
    """Send the buffered-stream keepalive after 20ms instead of 15s."""
    monkeypatch.setattr(completions, "_FORCE_STREAMING_KEEPALIVE_S", 0.02)


def _error_body(response) -> dict[str, Any]:
    # Keepalive whitespace precedes the JSON; parsers ignore it, so may we.
    assert response.content[:1].isspace(), "a keepalive byte was expected first"
    return json.loads(response.content)


async def test_an_upstream_crash_after_a_keepalive_ends_in_an_error_body(
    monkeypatch, quick_keepalive
):
    """The 200 is committed by then; the body must say what happened, scrubbed."""
    adapter = RecordingAdapter(
        "alpha",
        stream_script=[0.2, RuntimeError("upstream exploded: secret-internal-detail")],
    )
    app = _app(monkeypatch, [adapter], force_streaming=True)

    response = await _post(app)

    assert response.status_code == 200
    body = _error_body(response)
    assert body["error"]["code"] == 500
    assert "secret-internal-detail" not in response.text


async def test_the_buffer_turns_a_late_exception_into_an_error_body(quick_keepalive):
    """Defence in depth, so called directly: ``StreamSession`` already turns
    failures into error events, and this is what is left if it ever does not.
    Once a keepalive byte has committed the 200, raising would cut the body off.
    """

    async def _late_failure():
        await asyncio.sleep(0.2)
        raise RuntimeError("stream state broke: secret-internal-detail")
        yield  # pragma: no cover - makes this an async generator

    body = b"".join(
        [
            part
            async for part in completions._streaming_response_with_keepalive(
                _late_failure(), request_id="req-late", model=_MODEL, request_headers={}
            )
        ]
    )

    assert body[:1].isspace()
    error = json.loads(body)["error"]
    assert error["code"] == 500
    assert "secret-internal-detail" not in body.decode()


async def test_the_buffer_completes_the_usage_it_returns():
    """Its own guarantee, not only the stream session's: a body with valid usage."""

    async def _chunks():
        for chunk in _stream_with_usage({"prompt_tokens": 5, "completion_tokens": 3}):
            yield chunk

    body = b"".join(
        [
            part
            async for part in completions._streaming_response_with_keepalive(
                _chunks(), request_id="req-usage", model=_MODEL, request_headers={}
            )
        ]
    )

    usage = json.loads(body)["usage"]
    CompletionUsage.model_validate(usage)
    assert usage["total_tokens"] == 8


async def test_the_buffer_raises_a_failure_before_any_byte(quick_keepalive):
    """Nothing committed yet: raising lets the route answer with a real status."""

    async def _early_failure():
        raise RuntimeError("no upstream")
        yield  # pragma: no cover - makes this an async generator

    with pytest.raises(RuntimeError, match="no upstream"):
        async for _ in completions._streaming_response_with_keepalive(
            _early_failure(), request_id="req-early", model=_MODEL, request_headers={}
        ):
            pass


async def test_an_upstream_error_event_after_a_keepalive_ends_in_an_error_body(
    monkeypatch, quick_keepalive
):
    error_event = 'data: {"error": {"message": "rate limited upstream", "code": 429}}\n\n'
    adapter = RecordingAdapter("alpha", stream_script=[0.2, error_event])
    app = _app(monkeypatch, [adapter], force_streaming=True)

    response = await _post(app)

    assert response.status_code == 200
    body = _error_body(response)
    assert body["error"]["code"] == 429


async def test_a_slow_upstream_is_kept_alive_and_still_answers(monkeypatch, quick_keepalive):
    adapter = RecordingAdapter(
        "alpha",
        stream_script=[
            0.1,
            RecordingAdapter("alpha").format_stream_chunk(model=_MODEL, content="late "),
            0.1,
            RecordingAdapter("alpha").format_stream_chunk(model=_MODEL, content="answer"),
            done_sentinel(),
        ],
    )
    app = _app(monkeypatch, [adapter], force_streaming=True)

    response = await _post(app)

    assert response.status_code == 200
    assert response.content[:1].isspace()
    assert json.loads(response.content)["choices"][0]["message"]["content"] == "late answer"
